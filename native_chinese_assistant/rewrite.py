"""Rewrite service: LLM client + heuristic fallback.

Public surface kept stable for tests:
  validate_text, parse_variety, MAX_INPUT_CHARS, load_dotenv, load_llm_config,
  default_ca_bundle, extract_streamed_content, HeuristicRewriter, RewriteService,
  ChatCompletionsClient, RewriteError, RewriteResult, LLMConfig.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request

from native_chinese_assistant.presets import (
    PRESET_METADATA,
    SCENARIO_METADATA,
    PresetMetadata,
    Scenario,
    Script,
    VarietyPreset,
)

logger = logging.getLogger("ncga.rewrite")

MAX_INPUT_CHARS = 1200
MAX_OUTPUT_CHARS = 1600  # input + breathing room; matches "input + 200" prompt rule with margin
LLM_RETRIES = 1  # retry once on parse failure with stricter instruction


class RewriteError(Exception):
    """Raised when rewrite generation fails."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    streaming: bool
    ca_bundle: str | None
    skip_ssl_verify: bool
    timeout_seconds: float


@dataclass(frozen=True)
class RewriteResult:
    rewritten_text: str
    target_variety: VarietyPreset
    script: Script
    warning: str | None = None
    degraded: bool = False  # True if heuristic fallback was used (UI surfaces this prominently)

    def as_dict(self) -> dict[str, Any]:
        # asdict serializes enums via their .value when they subclass str — both Script & VarietyPreset do.
        payload = asdict(self)
        payload["target_variety"] = self.target_variety.value
        payload["script"] = self.script.value
        if not self.warning:
            payload.pop("warning", None)
        return payload


# ---------- text helpers ----------


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_text(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        raise ValueError("Text must not be empty.")
    if len(normalized) > MAX_INPUT_CHARS:
        raise ValueError(f"Text must be at most {MAX_INPUT_CHARS} characters.")
    return normalized


def parse_variety(value: str) -> VarietyPreset:
    try:
        return VarietyPreset(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported target variety: {value!r}.") from exc


# ---------- prompt construction ----------


_SYSTEM_PROMPT_HEADER = (
    "你是{label}的母语者，从小在当地长大，平时跟家里人和老乡都用这种话聊天。"
    "现在请把用户给你的中文，重写成你平时讲话的样子，让它读起来像【本地人在你耳边说】，"
    "而不是新闻稿、教科书或翻译腔。\n\n"
)

_SYSTEM_PROMPT_RULES = (
    "硬性规则：\n"
    "1. 保留原意、人名、地名、数字、日期、URL、专有名词不变；不要新增主观信息或替换人物。\n"
    "2. 改写后字符数 <= 原文字符数 + 200，超出请通过删去多余修饰、合并短句来精简。\n"
    "3. 不要逐字对换。优先调整句式、节奏、语气词，让句子有自然的呼吸。\n"
    "4. 大量使用目标方言的语气词、俚语、惯用表达、句末助词，但每句最多 1~2 个，避免堆砌。\n"
    "5. 称呼、敬语、玩笑分寸要符合当地习惯（如东北话直率热络、上海话精致克制）。\n"
    "6. 不要添加【以下是改写】【翻译为】之类的前导语；不要解释自己做了什么。\n"
    "7. 严格按以下 JSON 输出，不要 Markdown、不要多余字段：\n"
    '   {"rewritten_text": "...", "warning": ""}\n'
    "8. warning 仅在确实无法完全达到方言风味时，用 <=20 个汉字写一句简短说明"
    '（如：语料不足，已尽量贴近），否则一律填空字符串 ""。\n\n'
)

_SYSTEM_PROMPT_FOOTER = (
    "动笔前请先想一遍：如果是当地朋友在饭桌上、微信里跟我讲这件事，他会怎么说？"
    "记住——地道的核心是【听起来像人话】，而不是【塞满方言词】。"
)


def build_system_prompt(
    metadata: PresetMetadata,
    *,
    scenario: Scenario = Scenario.FRIENDS_CASUAL,
    strict_json: bool = False,
    addendum_override: str | None = None,
    glossary_lines: list[str] | None = None,
) -> str:
    """Compose the system prompt.

    Cycle 9 additions:
    - addendum_override: if provided, replaces the scenario's default prompt_addendum
      (used by the self-improving prompt system)
    - glossary_lines: brand-voice term substitutions, one per line as
      "中文 → 方言写法"; injected as an extra rule
    """
    # f-string: no .format() ⇒ literal `{` / `}` in metadata strings can never break templating.
    header = _SYSTEM_PROMPT_HEADER.replace("{label}", metadata.label)
    scenario_meta = SCENARIO_METADATA[scenario]
    addendum = addendum_override or scenario_meta.prompt_addendum
    body = (
        f"{header}{_SYSTEM_PROMPT_RULES}"
        f"【口吻定位】{metadata.register}\n"
        f"【风格指引】{metadata.style_notes}\n\n"
        f"{addendum}\n\n"
    )
    if glossary_lines:
        joined = "\n".join(f"  · {ln}" for ln in glossary_lines if ln.strip())
        if joined:
            body += f"【品牌语调字典】（必须严格执行的词汇映射）\n{joined}\n\n"
    body += _SYSTEM_PROMPT_FOOTER
    if strict_json:
        body += (
            "\n\n⚠ 上一次输出未通过 JSON 校验。"
            '本次必须返回纯 JSON 对象（{"rewritten_text": "...", "warning": ""}），'
            "不要任何包裹文本或 markdown。"
        )
    return body


# --- Rate-quality prompt (Cycle 8) ---

_RATE_SYSTEM_PROMPT = (
    "你是{label}的母语者，专门评估别人写出来的{label}地不地道。"
    "学生递来一段{label}，请你打分：0-5 分（0=完全不像方言，5=完全像本地人讲的）。\n\n"
    "硬性规则：\n"
    "1. 只看这段{label}本身，不要去脑补它原意是什么、跟谁讲。\n"
    "2. 0=普通话原文几乎没改；2-3=有方言味但还能改进；5=听起来跟本地人一字不差。\n"
    "3. 给一句 ≤20 字的简短理由，要点出最关键的方言/反方言特征。\n"
    '4. 严格 JSON：{{"score": 0-5, "reason": "..."}}\n'
    "5. 不要 markdown，不要解释自己。\n"
)


def build_rate_system_prompt(metadata: PresetMetadata) -> str:
    return _RATE_SYSTEM_PROMPT.replace("{label}", metadata.label).replace("{{", "{").replace("}}", "}")


# --- Explain prompt (Cycle 3) ---

_EXPLAIN_SYSTEM_PROMPT = (
    "你是一位{label}研究者兼语言老师。学生刚把一段普通话改写成了{label}。"
    "请你帮他对照原文与改写，挑出最有【方言味】的 3-6 处变化，并简明解释为什么这样写更地道。\n\n"
    "硬性规则：\n"
    "1. 只看原文与改写的【实际差异】，不要编造没出现的词。\n"
    "2. 每条 ≤ 25 个字，要点出【原文里的词/句式 → 改写里的词/句式 → 为什么】。\n"
    "3. 优先点出：方言专有词、句末助词、语序变化、语气变化、文化梗。\n"
    "4. 严格 JSON 输出，不要 markdown，不要前导语：\n"
    '   {{"summary": "一句话总评", "points": [{{"from": "...", "to": "...", "why": "..."}}, ...]}}\n'
    '5. 如果原文与改写【几乎相同】，summary 写"差异很小"，points 用空数组 []。\n'
)


def build_explain_system_prompt(metadata: PresetMetadata) -> str:
    return _EXPLAIN_SYSTEM_PROMPT.replace("{label}", metadata.label).replace("{{", "{").replace("}}", "}")


def build_explain_user_prompt(original: str, rewritten: str) -> str:
    return f"原文：\n{original}\n\n改写：\n{rewritten}"


def build_user_prompt(text: str, target: VarietyPreset) -> str:
    metadata = PRESET_METADATA[target]
    return f"请改写为{metadata.label}：\n{text}"


# ---------- env / config ----------


def load_dotenv(dotenv_path: str = ".env") -> None:
    path = Path(dotenv_path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), raw_value.strip().strip("'\""))


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default %s", name, raw, default)
        return default


def default_ca_bundle() -> str | None:
    override = os.environ.get("LLM_CA_BUNDLE", "").strip()
    if override:
        return override
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("certifi not installed; falling back to system trust store")
        return None
    return certifi.where()


def load_llm_config() -> LLMConfig | None:
    load_dotenv()
    provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
    api_key = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    default_base_url = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"
    default_model = "deepseek-chat" if provider == "deepseek" else "gpt-4.1-mini"
    base_url = os.environ.get("LLM_BASE_URL", default_base_url).strip()
    model = os.environ.get("LLM_MODEL", default_model).strip()
    streaming = env_flag("LLM_STREAM", True)
    skip_ssl_verify = env_flag("LLM_SKIP_SSL_VERIFY", False)
    timeout_seconds = env_float("LLM_TIMEOUT_SECONDS", 60.0)

    if skip_ssl_verify:
        logger.warning(
            "LLM_SKIP_SSL_VERIFY=true — TLS certificate validation is DISABLED. "
            "This is dangerous; only use locally behind a corporate proxy."
        )

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        streaming=streaming,
        ca_bundle=default_ca_bundle(),
        skip_ssl_verify=skip_ssl_verify,
        timeout_seconds=timeout_seconds,
    )


# ---------- LLM response parsing ----------


def extract_message_content(raw: dict[str, Any]) -> str:
    try:
        return raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RewriteError("LLM returned an invalid payload.") from exc


def extract_streamed_content(response: Any) -> str:
    chunks: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RewriteError("LLM stream returned invalid JSON.") from exc
        try:
            delta = event["choices"][0]["delta"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RewriteError("LLM stream returned an invalid event.") from exc
        content = delta.get("content")
        if content:
            chunks.append(content)
    if not chunks:
        raise RewriteError("LLM stream returned no content.")
    return "".join(chunks)


def iter_streamed_deltas(response: Any):
    """Generator yielding (delta_text, done) pairs from an OpenAI-format SSE response.

    Yields ('hello', False), (' world', False), ('', True). 'done' is True for the
    terminating event after which the generator stops.
    """
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            yield "", True
            return
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RewriteError("LLM stream returned invalid JSON.") from exc
        try:
            delta = event["choices"][0]["delta"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RewriteError("LLM stream returned an invalid event.") from exc
        content = delta.get("content") or ""
        if content:
            yield content, False
    # Stream ended without [DONE] sentinel — still emit done so caller knows we're finished.
    yield "", True


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_PARTIAL_REWRITTEN_RE = re.compile(r'"rewritten_text"\s*:\s*"((?:[^"\\]|\\.)*)', re.DOTALL)


def _try_extract_rewritten_text(partial_json: str) -> str:
    """Best-effort: extract whatever has been streamed so far for `rewritten_text`,
    even if the JSON object is incomplete (closing brace not yet received).

    Used to render progressive output character-by-character. If extraction fails,
    returns empty string (caller should keep showing previous best).
    """
    m = _PARTIAL_REWRITTEN_RE.search(partial_json)
    if not m:
        return ""
    raw = m.group(1)
    # Decode JSON escapes (\\n, \", \\u1234) without choking on incomplete escapes at the tail.
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        # Trailing partial escape — drop the last escape and try again
        cleaned = re.sub(r"\\.?$", "", raw)
        try:
            return json.loads(f'"{cleaned}"')
        except json.JSONDecodeError:
            return raw  # last resort: return as-is


def _parse_llm_json(content: str) -> dict[str, Any]:
    """Tolerate accidental code fences / preamble. Attempt strict parse first, then salvage."""
    stripped = content.strip()
    # Strip ```json fences
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(stripped)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RewriteError("LLM returned non-object JSON.")
    return parsed


# ---------- HTTP transport (wrappable for tests) ----------


class HTTPTransport(Protocol):
    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
        ssl_context: ssl.SSLContext,
    ) -> Any: ...


class UrllibTransport:
    """Default transport. Tests inject a fake to avoid real network."""

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
        ssl_context: ssl.SSLContext,
    ) -> Any:
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            return request.urlopen(req, timeout=timeout, context=ssl_context)  # noqa: S310
        except error.URLError as exc:
            raise RewriteError(f"LLM request failed: {exc.reason}") from exc


# ---------- LLM client ----------


class ChatCompletionsClient:
    def __init__(self, config: LLMConfig, transport: HTTPTransport | None = None) -> None:
        self.config = config
        self.ssl_context = self._build_ssl_context()
        self._transport: HTTPTransport = transport or UrllibTransport()

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self.config.skip_ssl_verify:
            context = ssl._create_unverified_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        if self.config.ca_bundle:
            return ssl.create_default_context(cafile=self.config.ca_bundle)
        return ssl.create_default_context()

    def _build_payload(
        self,
        text: str,
        target: VarietyPreset,
        *,
        scenario: Scenario,
        strict_json: bool,
        addendum_override: str | None = None,
        glossary_lines: list[str] | None = None,
    ) -> dict[str, Any]:
        metadata = PRESET_METADATA[target]
        return {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": build_system_prompt(
                        metadata,
                        scenario=scenario,
                        strict_json=strict_json,
                        addendum_override=addendum_override,
                        glossary_lines=glossary_lines,
                    ),
                },
                {"role": "user", "content": build_user_prompt(text, target)},
            ],
            "response_format": {"type": "json_object"},
            "stream": self.config.streaming,
            "temperature": 0.7,
            "max_tokens": 2048,
            "top_p": 0.9,
        }

    def _call_once(
        self,
        text: str,
        target: VarietyPreset,
        *,
        scenario: Scenario,
        strict_json: bool,
        addendum_override: str | None = None,
        glossary_lines: list[str] | None = None,
    ) -> str:
        payload = self._build_payload(
            text,
            target,
            scenario=scenario,
            strict_json=strict_json,
            addendum_override=addendum_override,
            glossary_lines=glossary_lines,
        )
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.config.streaming else "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"
        with self._transport.post(
            url, body, headers, timeout=self.config.timeout_seconds, ssl_context=self.ssl_context
        ) as response:
            if self.config.streaming:
                return extract_streamed_content(response)
            raw = json.loads(response.read().decode("utf-8"))
            return extract_message_content(raw)

    def rewrite_stream(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
    ):
        """Yield (raw_partial_json_text, partial_extracted_text, is_done) tuples.

        Every chunk yielded is whatever the LLM has streamed so far, *plus* an
        attempt to extract `rewritten_text` from the partial JSON if any.
        Final yield has is_done=True with the full RewriteResult.

        This streams without retry: streaming + retry is complex and the JSON-repair
        retry path is rarely needed in practice (LLMs usually output JSON cleanly
        when response_format is json_object).
        """
        metadata = PRESET_METADATA[target]
        # Force streaming on for this call
        if not self.config.streaming:
            # If the configured base config has streaming disabled, do a non-streaming
            # call and yield the result as a single chunk. Caller's UX still benefits
            # from a unified streaming interface.
            try:
                result = self.rewrite(text, target, scenario=scenario)
            except RewriteError:
                raise
            yield result.rewritten_text, result.rewritten_text, False
            yield "", "", True  # actually done; caller should also have full result via separate path
            return

        payload = self._build_payload(text, target, scenario=scenario, strict_json=False)
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self.config.base_url}/chat/completions"
        accumulated = ""
        with self._transport.post(
            url, body, headers, timeout=self.config.timeout_seconds, ssl_context=self.ssl_context
        ) as response:
            for delta, done in iter_streamed_deltas(response):
                if done:
                    break
                accumulated += delta
                # Try to extract a partial rewritten_text, even from incomplete JSON
                partial_text = _try_extract_rewritten_text(accumulated)
                yield delta, partial_text, False

        # Accumulated holds the full JSON. Parse strictly now.
        try:
            parsed = _parse_llm_json(accumulated)
        except (json.JSONDecodeError, RewriteError) as exc:
            raise RewriteError(f"LLM returned an invalid stream payload: {exc}") from exc
        rewritten_text = normalize_text(parsed.get("rewritten_text", ""))
        if not rewritten_text:
            raise RewriteError("LLM returned empty rewritten_text.")
        truncate_warning: str | None = None
        if len(rewritten_text) > MAX_OUTPUT_CHARS:
            rewritten_text = rewritten_text[:MAX_OUTPUT_CHARS]
            truncate_warning = "Result truncated to max length."
        raw_warning = (parsed.get("warning") or "").strip() or None
        warning = " | ".join(filter(None, [raw_warning, truncate_warning])) or None
        result = RewriteResult(
            rewritten_text=rewritten_text,
            target_variety=target,
            script=metadata.script,
            warning=warning,
            degraded=False,
        )
        yield "", rewritten_text, True
        # Caller can use the last yielded partial_text + use this returned value via .send/etc.
        # Simpler: caller calls rewrite_stream() then knows last partial is the final.
        return

    def rewrite(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
        *,
        addendum_override: str | None = None,
        glossary_lines: list[str] | None = None,
    ) -> RewriteResult:
        metadata = PRESET_METADATA[target]
        last_exc: Exception | None = None
        for attempt in range(LLM_RETRIES + 1):
            strict = attempt > 0
            try:
                content = self._call_once(
                    text,
                    target,
                    scenario=scenario,
                    strict_json=strict,
                    addendum_override=addendum_override,
                    glossary_lines=glossary_lines,
                )
                parsed = _parse_llm_json(content)
                rewritten_text = normalize_text(parsed.get("rewritten_text", ""))
                if not rewritten_text:
                    raise RewriteError("LLM returned empty rewritten_text.")

                truncate_warning: str | None = None
                if len(rewritten_text) > MAX_OUTPUT_CHARS:
                    rewritten_text = rewritten_text[:MAX_OUTPUT_CHARS]
                    truncate_warning = "Result truncated to max length."

                raw_warning = (parsed.get("warning") or "").strip() or None
                warning = " | ".join(filter(None, [raw_warning, truncate_warning])) or None
                return RewriteResult(
                    rewritten_text=rewritten_text,
                    target_variety=target,
                    script=metadata.script,
                    warning=warning,
                    degraded=False,
                )
            except (RewriteError, json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
                logger.info("LLM parse attempt %d/%d failed: %s", attempt + 1, LLM_RETRIES + 1, exc)
                continue

        raise RewriteError(f"LLM returned an invalid payload after retries: {last_exc}")

    def general_chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 800,
        temperature: float = 0.4,
        response_format_json: bool = True,
        retries_on_5xx: int = 1,
    ) -> str:
        """Generic chat-completions wrapper with HTTPError-aware retry on 5xx.

        Cycle 14: this replaces direct `_transport.post` access from rate_quality and
        meta_refine. Centralizes:
          - SSL context resolution
          - Timeout
          - Auth header
          - 5xx retry with exponential backoff (4xx not retried — request itself is bad)
          - Network-error retry (URLError that isn't HTTPError)
        Returns the raw `content` string from the first choice's message.
        """
        import time as _t
        from urllib.error import HTTPError, URLError

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.9,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"

        last_exc: Exception | None = None
        attempts = retries_on_5xx + 1
        for attempt in range(attempts):
            try:
                with self._transport.post(
                    url,
                    body,
                    headers,
                    timeout=self.config.timeout_seconds,
                    ssl_context=self.ssl_context,
                ) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                    return extract_message_content(raw)
            except HTTPError as exc:  # subclass of URLError — must catch first
                # 5xx → retry; 4xx → fail fast
                if 500 <= exc.code < 600 and attempt < attempts - 1:
                    backoff = 2**attempt  # 1s, 2s, 4s
                    logger.info(
                        "general_chat HTTP %d on attempt %d/%d; retrying in %ds",
                        exc.code,
                        attempt + 1,
                        attempts,
                        backoff,
                    )
                    last_exc = exc
                    _t.sleep(backoff)
                    continue
                raise RewriteError(f"LLM HTTP {exc.code}: {exc.reason}") from exc
            except URLError as exc:
                # Pure network error — retry once
                if attempt < attempts - 1:
                    backoff = 2**attempt
                    logger.info(
                        "general_chat network error on attempt %d/%d (%s); retrying in %ds",
                        attempt + 1,
                        attempts,
                        exc.reason,
                        backoff,
                    )
                    last_exc = exc
                    _t.sleep(backoff)
                    continue
                raise RewriteError(f"LLM network error: {exc.reason}") from exc
            except RewriteError:
                # Transport already wrapped in RewriteError — propagate without retry
                raise
        raise RewriteError(f"general_chat exhausted retries: {last_exc}")

    def rate_quality(self, rewritten: str, target: VarietyPreset) -> dict[str, Any]:
        """Score 0-5 how natively `rewritten` reads in `target`. Returns {score, reason}."""
        metadata = PRESET_METADATA[target]
        # Cycle 14: routes through general_chat, gets free 5xx retry + clean error path.
        # Cycle 17: max_tokens bumped 100→400. DeepSeek-V4 family burns reasoning tokens
        # before emitting JSON; 100 truncated the response to '' and rate silently broke.
        content = self.general_chat(
            [
                {"role": "system", "content": build_rate_system_prompt(metadata)},
                {"role": "user", "content": rewritten},
            ],
            max_tokens=400,
            temperature=0.2,
        )
        if not content or not content.strip():
            raise RewriteError("LLM returned empty content for rate.")
        try:
            parsed = _parse_llm_json(content)
        except (json.JSONDecodeError, RewriteError) as exc:
            raise RewriteError(f"Invalid rate JSON: {exc}") from exc
        try:
            score = float(parsed.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(5.0, score))
        reason = str(parsed.get("reason", "")).strip()[:80]
        return {"score": round(score, 1), "reason": reason}

    def explain(
        self,
        original: str,
        rewritten: str,
        target: VarietyPreset,
    ) -> dict[str, Any]:
        """Return {summary, points} explaining the rewrite.

        Resilience tiers:
        1. Parse strict JSON → return {summary, points}.
        2. JSON parse fails but content non-empty → return {markdown: content} (frontend renders).
        3. Empty content → raise RewriteError so the user sees a clear error.
        """
        metadata = PRESET_METADATA[target]
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": build_explain_system_prompt(metadata)},
                {"role": "user", "content": build_explain_user_prompt(original, rewritten)},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "temperature": 0.4,
            "max_tokens": 600,
            "top_p": 0.9,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"
        with self._transport.post(
            url, body, headers, timeout=self.config.timeout_seconds, ssl_context=self.ssl_context
        ) as response:
            raw = json.loads(response.read().decode("utf-8"))
            content = extract_message_content(raw)

        if not content or not content.strip():
            raise RewriteError("LLM returned empty content for explain.")

        try:
            parsed = _parse_llm_json(content)
        except (json.JSONDecodeError, RewriteError):
            # Soft-fail: hand the raw text to the frontend as markdown.
            logger.info("explain: JSON parse failed, falling back to markdown")
            return {"summary": "", "points": [], "markdown": content.strip()}

        # Defensively coerce shape.
        summary = str(parsed.get("summary", "")).strip()
        points_in = parsed.get("points") or []
        points: list[dict[str, str]] = []
        if isinstance(points_in, list):
            for p in points_in:
                if not isinstance(p, dict):
                    continue
                points.append(
                    {
                        "from": str(p.get("from", "")).strip(),
                        "to": str(p.get("to", "")).strip(),
                        "why": str(p.get("why", "")).strip(),
                    }
                )
        return {"summary": summary, "points": points}


# ---------- heuristic fallback ----------


class HeuristicRewriter:
    REPLACEMENTS: dict[VarietyPreset, list[tuple[str, str]]] = {
        VarietyPreset.STANDARD_PUTONGHUA: [("挺", "很"), ("贼", "特别")],
        VarietyPreset.BEIJING_MANDARIN: [("非常", "特"), ("朋友", "哥们儿"), ("真的", "真")],
        VarietyPreset.DONGBEI_MANDARIN: [("非常", "老"), ("特别", "老"), ("不错", "挺带劲")],
        VarietyPreset.SICHUAN_CHONGQING_MANDARIN: [("很", "蛮"), ("特别", "巴适得很")],
        VarietyPreset.JIANGHUAI_OR_LOWER_YANGTZE_MANDARIN: [("很", "蛮"), ("可以", "蛮可以")],
        VarietyPreset.GUANGDONG_MANDARIN: [("一起", "一齐"), ("厉害", "犀利")],
        VarietyPreset.SHANGHAI_MANDARIN_STYLE: [("马上", "立刻"), ("安排", "弄好")],
        VarietyPreset.CANTONESE_WRITTEN: [
            ("什么", "咩"),
            ("是不是", "係唔係"),
            ("很", "好"),
            ("的", "嘅"),
        ],
        VarietyPreset.HOKKIEN_WRITTEN: [("我们", "咱"), ("真的", "真个")],
        VarietyPreset.MINNAN_WRITTEN: [("我们", "阮"), ("你", "汝")],
    }

    PREFIXES: dict[VarietyPreset, str] = {
        VarietyPreset.BEIJING_MANDARIN: "给你捯饬成北京味儿：",
        VarietyPreset.DONGBEI_MANDARIN: "给你整成东北那股劲儿：",
        VarietyPreset.SICHUAN_CHONGQING_MANDARIN: "给你顺成川渝这边更自然的说法：",
        VarietyPreset.JIANGHUAI_OR_LOWER_YANGTZE_MANDARIN: "给你改成江淮一带更顺口的说法：",
        VarietyPreset.GUANGDONG_MANDARIN: "给你改成广东人讲普通话更自然的味道：",
        VarietyPreset.SHANGHAI_MANDARIN_STYLE: "给你理成上海这边更利落的说法：",
        VarietyPreset.CANTONESE_WRITTEN: "帮你改成书面上都顺眼嘅广东话：",
        VarietyPreset.HOKKIEN_WRITTEN: "帮你顺成带台湾腔、读起来较自然个写法：",
        VarietyPreset.MINNAN_WRITTEN: "帮你顺成带闽南味、读起来较自然个写法：",
    }

    def rewrite(self, text: str, target: VarietyPreset, reason: str | None = None) -> RewriteResult:
        metadata = PRESET_METADATA[target]
        rewritten = text
        for source, replacement in self.REPLACEMENTS.get(target, []):
            rewritten = rewritten.replace(source, replacement)
        if rewritten == text and target != VarietyPreset.STANDARD_PUTONGHUA:
            rewritten = f"{self.PREFIXES.get(target, '帮你改写成更自然的说法：')}{text}"
        warning_reason = reason or "No LLM provider is configured."
        warning = f"Using local heuristic fallback. {warning_reason} {metadata.fallback_warning}"
        return RewriteResult(
            rewritten_text=rewritten,
            target_variety=target,
            script=metadata.script,
            warning=warning,
            degraded=True,
        )


# ---------- service ----------


class RewriteService:
    def __init__(
        self,
        *,
        config: LLMConfig | None = None,
        client: ChatCompletionsClient | None = None,
        quality_store: Any = None,  # native_chinese_assistant.feedback.QualityStore | None
    ) -> None:
        # Allow tests / callers to inject either a ready client or a synthetic config.
        if client is not None:
            self._config = client.config
            self._client: ChatCompletionsClient | None = client
        else:
            self._config = config if config is not None else load_llm_config()
            self._client = ChatCompletionsClient(self._config) if self._config else None
        self._fallback = HeuristicRewriter()
        self.quality_store = quality_store  # if None, no self-improvement; otherwise lookup overrides

    def rewrite(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
        *,
        glossary_lines: list[str] | None = None,
    ) -> RewriteResult:
        normalized = validate_text(text)
        started = time.perf_counter()
        addendum_override = None
        if self.quality_store is not None:
            addendum_override = self.quality_store.get_override_addendum(target.value, scenario.value)
        if self._client is None:
            result = self._fallback.rewrite(normalized, target, reason="No LLM provider is configured.")
            self._log_request(target, started, degraded=True)
            return result

        try:
            result = self._client.rewrite(
                normalized,
                target,
                scenario=scenario,
                addendum_override=addendum_override,
                glossary_lines=glossary_lines,
            )
            self._log_request(target, started, degraded=result.degraded)
            return result
        except RewriteError as exc:
            logger.warning("LLM rewrite failed; using heuristic fallback: %s", exc)
            result = self._fallback.rewrite(normalized, target, reason=str(exc))
            self._log_request(target, started, degraded=True)
            return result

    def rate_quality(
        self,
        rewritten: str,
        target: VarietyPreset,
        *,
        scenario: Scenario | None = None,
        original: str = "",
    ) -> dict[str, Any]:
        """Service wrapper: rate the native-ness of `rewritten` in `target`.

        Cycle 17: replaced the awkward `record_for=(variety_value, scenario_value)`
        tuple — the caller had to pass both `target` and `target.value`. Now the
        caller passes the `scenario` enum directly; we derive the storage keys.
        If `scenario` is provided and a quality_store is attached, the result is
        recorded for the self-improving loop.
        """
        if self._client is None:
            raise RewriteError("评分功能需要 LLM 服务。请先配置 LLM_API_KEY 后再试。")
        if not rewritten or not rewritten.strip():
            raise ValueError("rewritten must be non-empty")
        if len(rewritten) > MAX_OUTPUT_CHARS:
            raise ValueError("rewritten too long")
        result = self._client.rate_quality(rewritten, target)
        if self.quality_store is not None and scenario is not None:
            self.quality_store.record(
                variety=target.value,
                scenario=scenario.value,
                score=float(result.get("score", 0)),
                original=original,
                rewritten=rewritten,
                reason=str(result.get("reason", "")),
            )
        return result

    def meta_refine(self, target: VarietyPreset, scenario: Scenario) -> dict[str, Any]:
        """Reflexion-style: ask the LLM to look at hi/lo scoring samples for this
        (variety, scenario) and propose a refined prompt addendum.

        Returns {new_addendum, diff_summary, baseline_mean, sample_count}.
        Raises RewriteError if the store doesn't have enough data, or LLM fails.
        """
        from native_chinese_assistant.feedback import (  # local import to avoid cycle
            REFINER_SYSTEM_PROMPT,
            build_refiner_user_prompt,
        )

        if self._client is None:
            raise RewriteError("自反馈功能需要 LLM 服务。请先配置 LLM_API_KEY 后再试。")
        if self.quality_store is None:
            raise RewriteError("尚未启用 quality store，无法进行自反馈。")
        bucket = self.quality_store.get_bucket(target.value, scenario.value)
        if not bucket or bucket.stats.n < self.quality_store.trigger_min_count:
            raise ValueError(
                f"样本不足（需 ≥ {self.quality_store.trigger_min_count}，目前 {bucket.stats.n if bucket else 0}）"
            )
        hi, lo = self.quality_store.hi_lo_examples(target.value, scenario.value)
        metadata = PRESET_METADATA[target]
        scenario_meta = SCENARIO_METADATA[scenario]
        current_addendum = bucket.override_addendum or scenario_meta.prompt_addendum
        user_prompt = build_refiner_user_prompt(
            variety_label=metadata.label,
            scenario_label=scenario_meta.label,
            current_addendum=current_addendum,
            hi_samples=hi,
            lo_samples=lo,
        )
        # Cycle 14: route through public general_chat (no more _transport peek).
        # Inherits 5xx + network retry + clean error wrapping for free.
        content = self._client.general_chat(
            [
                {"role": "system", "content": REFINER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,
            temperature=0.3,
        )
        try:
            parsed = _parse_llm_json(content)
        except (json.JSONDecodeError, RewriteError) as exc:
            raise RewriteError(f"Refiner returned invalid JSON: {exc}") from exc
        new_addendum = str(parsed.get("new_addendum", "")).strip()
        diff_summary = str(parsed.get("diff_summary", "")).strip()
        if not new_addendum:
            raise RewriteError("Refiner returned empty new_addendum.")
        baseline_mean = bucket.stats.mean
        # Cycle 10: write to DRAFT, not directly to active override.
        # User must explicitly call activate_override (via /api/override-activate) to apply.
        self.quality_store.set_draft(
            variety=target.value,
            scenario=scenario.value,
            addendum=new_addendum,
            reason=diff_summary or "auto-refined from quality samples",
            baseline_mean=baseline_mean,
        )
        return {
            "new_addendum": new_addendum,
            "diff_summary": diff_summary,
            "baseline_mean": round(baseline_mean, 2),
            "sample_count": bucket.stats.n,
            "hi_count": len(hi),
            "lo_count": len(lo),
            "status": "draft",  # explicit signal to caller: not yet active
        }

    def rewrite_stream(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
    ):
        """Stream chunks. Yields (delta_text, partial_text, is_done).

        On is_done=True, the stream is over; caller should check that partial_text
        is non-empty (otherwise treat as failure).
        Falls back gracefully: if no LLM configured, uses heuristic and emits one chunk.
        """
        normalized = validate_text(text)
        started = time.perf_counter()
        if self._client is None:
            result = self._fallback.rewrite(normalized, target, reason="No LLM provider is configured.")
            self._log_request(target, started, degraded=True)
            yield result.rewritten_text, result.rewritten_text, False
            yield "", result.rewritten_text, True
            return
        try:
            for delta, partial, done in self._client.rewrite_stream(normalized, target, scenario=scenario):
                yield delta, partial, done
                if done:
                    break
            self._log_request(target, started, degraded=False)
        except RewriteError as exc:
            logger.warning("LLM stream failed; falling back to heuristic: %s", exc)
            result = self._fallback.rewrite(normalized, target, reason=str(exc))
            self._log_request(target, started, degraded=True)
            yield result.rewritten_text, result.rewritten_text, False
            yield "", result.rewritten_text, True

    def explain(
        self,
        original: str,
        rewritten: str,
        target: VarietyPreset,
    ) -> dict[str, Any]:
        """Return an explanation of how the rewrite changed the original.

        Raises RewriteError if no LLM is configured (heuristic can't explain).
        """
        if self._client is None:
            raise RewriteError("解释功能需要 LLM 服务。请先配置 LLM_API_KEY 后再试。")
        if not original or not rewritten:
            raise ValueError("original and rewritten must both be non-empty")
        if len(original) > MAX_INPUT_CHARS or len(rewritten) > MAX_OUTPUT_CHARS:
            raise ValueError("text too long for explain")
        return self._client.explain(original, rewritten, target)

    def _log_request(self, target: VarietyPreset, started: float, *, degraded: bool) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        provider = self._config.provider if self._config else "heuristic"
        logger.info(
            "rewrite_request target=%s latency_ms=%s degraded=%s provider=%s",
            target.value,
            duration_ms,
            degraded,
            provider,
        )
