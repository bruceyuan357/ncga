"""Rewrite service: LLM client.

Public surface kept stable for tests:
  validate_text, parse_variety, MAX_INPUT_CHARS, load_dotenv, load_llm_config,
  default_ca_bundle, extract_streamed_content, RewriteService,
  ChatCompletionsClient, RewriteError, RewriteResult, LLMConfig.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import ssl
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
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


# Every user-facing message from this module is Chinese: the whole product is a
# Chinese-language UI, and the frontend renders `warning` / error strings verbatim.
# Leaking an English exception string into that UI is a bug, not a detail.
NO_LLM_MESSAGE = (
    "尚未配置 API Key，方言改写不可用。"
    "请在「设置 → API 密钥」粘贴你的 DeepSeek Key，或在服务器 .env 中设置 DEEPSEEK_API_KEY。"
)


class RewriteError(Exception):
    """Raised when rewrite generation fails."""


class EmptyStreamError(RewriteError):
    """The SSE stream completed with zero content deltas.

    Distinct from a JSON parse failure: this is transient upstream flakiness
    (observed on deepseek-v4-flash under parallel load — the stream opens,
    then closes with no tokens). The retry loop uses this marker to fall back
    to a plain non-streaming request instead of repeating the same flaky
    streaming call.
    """


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
    # Cycle 23: per-transform-mode model routing (mode-key -> model name),
    # built from LLM_MODEL_<MODE> env vars in load_llm_config.
    model_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RewriteResult:
    rewritten_text: str
    target_variety: VarietyPreset
    script: Script
    warning: str | None = None
    degraded: bool = False  # reserved: no code path sets this now that the fake-result fallback is gone

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
    "用户会用任何语言（中文、英文、日文、法文、韩文、西班牙文等）给你一段话，"
    "请你抓住意思，用你平时讲话的样子直接讲出来，"
    "让它读起来像【本地人在你耳边说】，而不是新闻稿、教科书或翻译腔。\n\n"
)

_SYSTEM_PROMPT_RULES = (
    "硬性规则：\n"
    "1. 输出语言锁定：无论用户用什么语言输入，输出必须是中文文字（{label}的本地表达），"
    "绝对不要返回英文、日文或其它非中文字符（人名、地名、URL、专有名词除外）。\n"
    "2. 跨语言一致性：同一个意思，无论用户用哪种语言提问，输出都应当高度一致——"
    "方言版本由【说话的内容和情境】决定，不要被【输入的语言形式】影响；"
    "不要逐词对译，要先在脑里抓住意思再用方言重说一遍。\n"
    "3. 保留原意、人名、地名、数字、日期、URL、专有名词不变；不要新增主观信息或替换人物。\n"
    "4. 输出汉字数 <= 用户输入信息所需的中文长度 + 200；中文输入按原文长度算，"
    "非中文输入按你脑中等价中文的合理长度算，超出请精简。\n"
    "5. 不要逐字对换。优先调整句式、节奏、语气词，让句子有自然的呼吸。\n"
    "6. 大量使用目标方言的语气词、俚语、惯用表达、句末助词，但每句最多 1~2 个，避免堆砌。\n"
    "7. 称呼、敬语、玩笑分寸要符合当地习惯（如东北话直率热络、上海话精致克制）。\n"
    "8. 不要添加【以下是改写】【翻译为】【原文/译文对照】之类的前导语或说明；"
    "不要保留原文、不要给双语对照；只输出最终的方言版本，仿佛本地人原本就是这么说的。\n"
    "9. 严格按以下 JSON 输出，不要 Markdown、不要多余字段：\n"
    '   {"rewritten_text": "...", "warning": ""}\n'
    "10. warning 仅在确实无法完全达到方言风味时，用 <=20 个汉字写一句简短说明"
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
    glossary_lines: list[str] | None = None,
    example_lines: list[str] | None = None,
    lexicon_lines: list[str] | None = None,
) -> str:
    """Compose the system prompt.

    - glossary_lines: brand-voice term substitutions, one per line as
      "中文 → 方言写法"; injected as an extra rule

    Cycle 22 Stage C addition:
    - example_lines: few-shot retrieved corpus pairs, one per line. Injected as
      【本地人示例】 block — they show the model concrete (原文 → 本地说法) so
      it doesn't have to guess what "上海话风格" feels like.
    """
    # f-string: no .format() ⇒ literal `{` / `}` in metadata strings can never break templating.
    header = _SYSTEM_PROMPT_HEADER.replace("{label}", metadata.label)
    scenario_meta = SCENARIO_METADATA[scenario]
    addendum = scenario_meta.prompt_addendum
    body = (
        f"{header}{_SYSTEM_PROMPT_RULES}"
        f"【口吻定位】{metadata.register}\n"
        f"【风格指引】{metadata.style_notes}\n\n"
        f"{addendum}\n\n"
    )
    if example_lines:
        # Filter empty/whitespace lines; injected RIGHT BEFORE glossary so the
        # model has concrete examples to absorb before any explicit rules.
        filtered = [ln for ln in example_lines if ln and ln.strip()]
        if filtered:
            joined = "\n".join(filtered)
            body += f"【本地人示例】（参考这些真实的本地说法,不要逐字复制,要学语气和句式）\n{joined}\n\n"
    if lexicon_lines:
        # Cycle 22 Stage D: dictionary-level word mappings retrieved from
        # data/lexicon.jsonl. Distinct from glossary (user-defined brand voice):
        # this is authoritative reference data sourced via deep research, used
        # as soft hints rather than hard substitutions.
        filtered = [ln for ln in lexicon_lines if ln and ln.strip()]
        if filtered:
            joined = "\n".join(filtered)
            body += (
                "【词音参考】（这些是该方言里常见的本地表达,如果你的改写里能自然用上几个就用,不要强塞）\n"
                f"{joined}\n\n"
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

# 2026-08: the judge now receives the target's own definition ({register})
# and scores against THAT, not its own guess. Without it, ambiguous varieties
# drift: 广东普通话 outputs got scored against written-Cantonese expectations
# (嘅/屋企/食 → 5.0) while spec-correct accented Mandarin (讲清楚点啦) scored
# 2.0 — the judge and the preset disagreed on what the variety even is.
_RATE_SYSTEM_PROMPT = (
    "你是{label}的母语者，专门评估别人写出来的{label}地不地道。"
    "学生递来一段{label}，请你打分：0-5 分（0=完全不像方言，5=完全像本地人讲的）。\n\n"
    "先给你「{label}」的判定标准（以此为准，不要按自己的想象另立标准）：\n{register}\n\n"
    "硬性规则：\n"
    "1. 只看这段{label}本身，不要去脑补它原意是什么、跟谁讲。\n"
    "2. 0=普通话原文几乎没改；2-3=有方言味但还能改进；5=听起来跟本地人一字不差。\n"
    "3. 给一句 ≤20 字的简短理由，要点出最关键的方言/反方言特征。\n"
    '4. 严格 JSON：{{"score": 0-5, "reason": "..."}}\n'
    "5. 不要 markdown，不要解释自己。\n"
)

# 2026-08: near-identity targets (标准普通话) need a different 0-anchor — the
# default rubric scores "和原文几乎一样" as 0, but for standard Mandarin that
# IS the correct output. The old rubric made every putonghua sweep row score
# near-zero regardless of quality (the 2.60/5 "weakest dialect" artifact).
_RATE_SYSTEM_PROMPT_NEAR_IDENTITY = (
    "你是{label}的母语者，专门评估别人写出来的{label}地不地道。"
    "学生递来一段{label}，请你打分：0-5 分（0=完全不像{label}，5=完全像本地人讲的）。\n\n"
    "先给你「{label}」的判定标准（以此为准，不要按自己的想象另立标准）：\n{register}\n\n"
    "硬性规则：\n"
    "1. 只看这段{label}本身，不要去脑补它原意是什么、跟谁讲。\n"
    "2. 这个目标语体和普通话原文本来就非常接近——和原文几乎一样不是缺点。"
    "0=画蛇添足、乱加方言味或语体错误；2-3=通顺但不够自然得体；5=自然、规范、语气到位。\n"
    "3. 给一句 ≤20 字的简短理由，要点出最关键的语体特征。\n"
    '4. 严格 JSON：{{"score": 0-5, "reason": "..."}}\n'
    "5. 不要 markdown，不要解释自己。\n"
)


# Cycle 21 self-audit #9: PRESET_METADATA is module-level + frozen, so for any
# given (label, register) the .replace() chain produces the same string every
# call. Cache by both strings. ~20µs per call avoided × ~10 calls/min.
@functools.lru_cache(maxsize=64)
def _rate_prompt_for_preset(label: str, register: str, near_identity_target: bool = False) -> str:
    template = _RATE_SYSTEM_PROMPT_NEAR_IDENTITY if near_identity_target else _RATE_SYSTEM_PROMPT
    return (
        template.replace("{label}", label)
        .replace("{register}", register)
        .replace("{{", "{")
        .replace("}}", "}")
    )


def build_rate_system_prompt(metadata: PresetMetadata, *, near_identity_target: bool = False) -> str:
    return _rate_prompt_for_preset(metadata.label, metadata.register, near_identity_target)


# --- Explain prompt (Cycle 3) ---

_EXPLAIN_SYSTEM_PROMPT = (
    "你是一位{label}研究者兼语言老师。学生刚把一段原文（可能是普通话，也可能是英文/日文等其它语言）"
    "改写成了{label}。请你帮他对照原文与改写，挑出最有【方言味】的 3-6 处变化，"
    "并简明解释为什么这样写更地道。\n\n"
    "硬性规则：\n"
    "1. 只看原文与改写的【实际差异】，不要编造没出现的词。\n"
    "2. 每条 ≤ 25 个字，要点出【原文里的词/句式 → 改写里的词/句式 → 为什么】；"
    "如果原文是非中文，可在 from 字段写原词的中文意译再加注原词，例:'tomorrow（明天）'。\n"
    "3. 优先点出：方言专有词、句末助词、语序变化、语气变化、文化梗。\n"
    "4. 用中文回答，不要在 summary / why 里夹杂大段英文。\n"
    "5. 严格 JSON 输出，不要 markdown，不要前导语：\n"
    '   {{"summary": "一句话总评", "points": [{{"from": "...", "to": "...", "why": "..."}}, ...]}}\n'
    '6. 如果原文与改写【几乎相同】，summary 写"差异很小"，points 用空数组 []。\n'
)


@functools.lru_cache(maxsize=32)
def _explain_prompt_for_label(label: str) -> str:
    return _EXPLAIN_SYSTEM_PROMPT.replace("{label}", label).replace("{{", "{").replace("}}", "}")


def build_explain_system_prompt(metadata: PresetMetadata) -> str:
    return _explain_prompt_for_label(metadata.label)


def build_explain_user_prompt(original: str, rewritten: str) -> str:
    return f"原文：\n{original}\n\n改写：\n{rewritten}"


# --- Cycle 18 (Function 1): 情境向导 — Conversational Setup ---
# User answers two questions (recipient + mood). LLM returns a small JSON with
# scenario suggestion, register hint, emotion tone, and up to 5 glossary entries.
_CHARACTERIZE_SYSTEM_PROMPT = (
    "你是中文方言重写助手的情境分析师。用户准备把一段话改成方言版本，"
    "他先告诉了你 (1) 这话要发给谁、(2) 他的心情/目的。"
    "你的任务：根据这两条信息，从下列固定枚举里挑一个最贴的【场景】，"
    "并给一段话的语气提示、情感色彩、最多 5 条品牌词建议。\n\n"
    "硬性规则：\n"
    "1. scenario 字段必须是下列之一：{scenario_values}\n"
    "2. register_hint ≤ 24 字，用一句话描述合适的语气（例：'温和但坚定，不要太书面'）。\n"
    "3. emotional_tone ≤ 12 字（例：'抱怨但克制'、'温暖关切'）。\n"
    "4. glossary_suggestions：≤ 5 条；每条 ≤ 30 字，格式 '原词 → 推荐词'，"
    "用于品牌语调字典。如果没有合适的就给空数组 []。\n"
    "5. 严格 JSON 输出，不要 markdown，不要前导语：\n"
    '   {{"suggested_scenario": "...", "register_hint": "...", '
    '"emotional_tone": "...", "glossary_suggestions": ["...", ...]}}\n'
)


@functools.lru_cache(maxsize=4)
def _characterize_prompt_for_joined(joined: str) -> str:
    return (
        _CHARACTERIZE_SYSTEM_PROMPT.replace("{scenario_values}", joined).replace("{{", "{").replace("}}", "}")
    )


def build_characterize_system_prompt(scenario_values: list[str]) -> str:
    # list → str so lru_cache key is hashable. The Scenario enum is small;
    # in practice this cache holds exactly 1 entry (the full enum joined).
    return _characterize_prompt_for_joined(" / ".join(scenario_values))


def build_characterize_user_prompt(recipient: str, mood: str) -> str:
    return (
        f"对方是谁：{recipient or '（未填）'}\n我的心情/目的：{mood or '（未填）'}\n请按系统说明给出 JSON。"
    )


def build_user_prompt(text: str, target: VarietyPreset) -> str:
    metadata = PRESET_METADATA[target]
    return f"用{metadata.label}讲下面这段话（输入语言不限，输出必须是中文{metadata.label}）：\n{text}"


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
        import certifi
    except ImportError:
        logger.debug("certifi not installed; falling back to system trust store")
        return None
    return certifi.where()


def load_llm_config() -> LLMConfig | None:
    load_dotenv()
    api_key = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    return _build_llm_config(api_key)


def llm_config_for_user_key(user_key: str) -> LLMConfig:
    """BYOK: build a config around a user-supplied API key.

    Reuses every server-side default (provider, base URL, model, per-mode
    overrides, timeouts, TLS settings) — only the credential changes. The key
    rides one request and is never persisted or logged.
    """
    load_dotenv()
    return _build_llm_config(user_key)


def _build_llm_config(api_key: str) -> LLMConfig:
    provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
    default_base_url = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"
    default_model = "deepseek-v4-flash" if provider == "deepseek" else "gpt-4.1-mini"
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

    # Cycle 23: any LLM_MODEL_<MODE>=name env becomes a transform-mode override
    # keyed by lowercased <MODE>. EXPLAIN defaults to the pro tier on deepseek —
    # explanation draws on world knowledge, where flash-tier models confidently
    # err (user decision; the other three modes stay on the global model).
    model_overrides: dict[str, str] = {}
    for env_key, env_value in os.environ.items():
        if env_key.startswith("LLM_MODEL_") and env_value.strip():
            model_overrides[env_key[len("LLM_MODEL_") :].lower()] = env_value.strip()
    if "explain" not in model_overrides and provider == "deepseek":
        model_overrides["explain"] = "deepseek-v4-pro"

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url.rstrip("/"),
        streaming=streaming,
        ca_bundle=default_ca_bundle(),
        skip_ssl_verify=skip_ssl_verify,
        timeout_seconds=timeout_seconds,
        model_overrides=model_overrides,
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
        # Cycle 21 self-audit #4: a network blip mid-stream can deliver bytes
        # that don't form valid UTF-8. Without this guard, UnicodeDecodeError
        # leaks past the caller, which only catches `RewriteError`. Wrap so
        # the failure-mapping is uniform — caller sees RewriteError and falls
        # back to the heuristic.
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RewriteError("LLM stream returned invalid UTF-8.") from exc
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
        raise EmptyStreamError("LLM stream returned no content.")
    return "".join(chunks)


def iter_streamed_deltas(response: Any) -> Iterator[tuple[str, bool]]:
    """Generator yielding (delta_text, done) pairs from an OpenAI-format SSE response.

    Yields ('hello', False), (' world', False), ('', True). 'done' is True for the
    terminating event after which the generator stops.
    """
    for raw_line in response:
        # Cycle 21 self-audit #4: same UTF-8 guard as extract_streamed_content.
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RewriteError("LLM stream returned invalid UTF-8.") from exc
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
# Cycle 21 self-audit #3: strip C0 control chars + DEL before user-supplied
# free-form strings reach the LLM. Without this, a user can put
# "\n\n---\nIGNORE PREVIOUS INSTRUCTIONS\n[NEW SYSTEM]:..." in `recipient`
# / `mood` and pry the system prompt open. _coerce_glossary already validates
# its own schema; characterize was the open vector.
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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


def _apply_thinking(payload: dict[str, Any], provider: str, thinking: str | None) -> None:
    """DeepSeek-V4 reasoning control for one payload.

    deepseek-v4-flash/pro are reasoning models: with thinking left on, the
    chain-of-thought can eat the whole max_tokens budget and return
    finish_reason=length with ZERO content — the root cause of the 2026-08
    "empty stream / empty content" degradations.

      None       → provider default (thinking on) — for tasks that want CoT
      "disabled" → thinking={type:disabled} — stylistic/generative tasks that
                   need no CoT; ~10x faster and immune to budget-eating
      "low"      → reasoning_effort=low — light reasoning at bounded cost

    DeepSeek-only fields (OpenAI 400s on unknown params), so other providers
    are left untouched.
    """
    if thinking is None or provider != "deepseek":
        return
    if thinking == "disabled":
        payload["thinking"] = {"type": "disabled"}
    elif thinking == "low":
        payload["reasoning_effort"] = "low"
    else:
        raise ValueError(f"unknown thinking mode: {thinking!r}")


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
        glossary_lines: list[str] | None = None,
    ) -> dict[str, Any]:
        metadata = PRESET_METADATA[target]
        # Cycle 22 Stage C: retrieve few-shot examples from corpus (zero-dep BM25).
        # Disabled when NCGA_CORPUS_DISABLE=1 or corpus file missing/empty.
        example_lines: list[str] | None = None
        try:
            from native_chinese_assistant.corpus import (
                format_examples_for_prompt,
                get_default_retriever,
            )

            retriever = get_default_retriever()
            if retriever is not None:
                hits = retriever.retrieve(text, target.value, top_k=3)
                if hits:
                    example_lines = format_examples_for_prompt(hits)
        except Exception:
            # Corpus is an enhancement; never fail rewrite because retrieval broke.
            example_lines = None
        # Cycle 22 Stage D: same defensive pattern for lexicon.
        # Disabled when NCGA_LEXICON_DISABLE=1 or lexicon file missing/empty.
        lexicon_lines: list[str] | None = None
        try:
            from native_chinese_assistant.lexicon import (
                format_lexicon_for_prompt,
                get_default_lexicon_retriever,
            )

            lex_retriever = get_default_lexicon_retriever()
            if lex_retriever is not None:
                lex_hits = lex_retriever.retrieve(text, target.value, top_k=5)
                if lex_hits:
                    lexicon_lines = format_lexicon_for_prompt(lex_hits)
        except Exception:
            lexicon_lines = None
        payload: dict[str, Any] = {
            "model": metadata.model or self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": build_system_prompt(
                        metadata,
                        scenario=scenario,
                        strict_json=strict_json,
                        glossary_lines=glossary_lines,
                        example_lines=example_lines,
                        lexicon_lines=lexicon_lines,
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
        if self.config.provider == "deepseek":
            # Per-variety reasoning control (PresetMetadata.reasoning):
            # Mandarin-family = "disabled" (no CoT needed, ~10x faster);
            # low-resource varieties = "low" (grammar transform wants light
            # planning). See _apply_thinking for the 2026-08 root cause.
            _apply_thinking(payload, self.config.provider, metadata.reasoning)
        return payload

    def _call_once(
        self,
        text: str,
        target: VarietyPreset,
        *,
        scenario: Scenario,
        strict_json: bool,
        glossary_lines: list[str] | None = None,
        stream: bool | None = None,
    ) -> str:
        payload = self._build_payload(
            text,
            target,
            scenario=scenario,
            strict_json=strict_json,
            glossary_lines=glossary_lines,
        )
        # `stream=None` → follow config; explicit bool overrides for one call
        # (the empty-stream retry path drops to plain non-streaming HTTP).
        use_stream = self.config.streaming if stream is None else stream
        payload["stream"] = use_stream
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if use_stream else "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"
        with self._transport.post(
            url, body, headers, timeout=self.config.timeout_seconds, ssl_context=self.ssl_context
        ) as response:
            if use_stream:
                return extract_streamed_content(response)
            raw = json.loads(response.read().decode("utf-8"))
            return extract_message_content(raw)

    def rewrite_stream(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
        *,
        glossary_lines: list[str] | None = None,
    ) -> Iterator[tuple[str, str, bool, dict[str, Any] | None]]:
        """Yield (delta, partial_extracted_text, is_done, meta) tuples.

        Every chunk yielded is whatever the LLM has streamed so far, *plus* an
        attempt to extract `rewritten_text` from the partial JSON if any.
        meta is None on chunks; the final is_done=True yield carries
        {"degraded": bool, "warning": str | None} so SSE consumers can forward
        the same contract as non-streaming /api/rewrite (review fix P3 — the
        old 3-tuple silently dropped warning/degraded on the primary UX path).

        Review fix P2: glossary_lines now threads into the payload exactly
        like the non-streaming rewrite() — previously the primary streaming
        path silently ignored it.

        This streams without retry: streaming + retry is complex and the JSON-repair
        retry path is rarely needed in practice (LLMs usually output JSON cleanly
        when response_format is json_object).
        """
        # Force streaming on for this call
        if not self.config.streaming:
            # If the configured base config has streaming disabled, do a non-streaming
            # call and yield the result as a single chunk. Caller's UX still benefits
            # from a unified streaming interface.
            try:
                result = self.rewrite(
                    text,
                    target,
                    scenario=scenario,
                    glossary_lines=glossary_lines,
                )
            except RewriteError:
                raise
            yield result.rewritten_text, result.rewritten_text, False, None
            # Final partial carries the full text (was "" — violated the
            # "empty partial at done means failure" contract for LLM_STREAM=false).
            yield "", result.rewritten_text, True, {"degraded": result.degraded, "warning": result.warning}
            return

        payload = self._build_payload(
            text,
            target,
            scenario=scenario,
            strict_json=False,
            glossary_lines=glossary_lines,
        )
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
                yield delta, partial_text, False, None

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
        yield "", rewritten_text, True, {"degraded": False, "warning": warning}
        return

    def rewrite(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
        *,
        glossary_lines: list[str] | None = None,
    ) -> RewriteResult:
        metadata = PRESET_METADATA[target]
        last_exc: Exception | None = None
        max_attempts = LLM_RETRIES + 1
        empty_stream_retried = False
        attempt = 0
        while attempt < max_attempts:
            strict = attempt > 0
            # An empty SSE stream is transient upstream flakiness specific to
            # streaming delivery (observed on deepseek-v4-flash under parallel
            # load). Repeating the identical streaming call just hits it again,
            # so retry over plain non-streaming HTTP — and grant one bonus
            # attempt, since transport flakiness shouldn't burn the
            # JSON-repair retry budget.
            stream_override: bool | None = False if isinstance(last_exc, EmptyStreamError) else None
            try:
                content = self._call_once(
                    text,
                    target,
                    scenario=scenario,
                    strict_json=strict,
                    glossary_lines=glossary_lines,
                    stream=stream_override,
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
                if isinstance(exc, EmptyStreamError) and not empty_stream_retried:
                    max_attempts += 1
                    empty_stream_retried = True
                last_exc = exc
                logger.info("LLM parse attempt %d/%d failed: %s", attempt + 1, max_attempts, exc)
                attempt += 1
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
        retry_on_empty: bool = True,
        model: str | None = None,
        thinking: str | None = None,
    ) -> str:
        """Generic chat-completions wrapper with HTTPError-aware retry on 5xx.

        Cycle 23: optional `model` overrides config.model for this one call —
        transform-mode routing (explain → pro tier) rides on this.

        2026-08: optional `thinking` ("disabled" | "low") rides on
        _apply_thinking — per-call DeepSeek reasoning control. None keeps the
        provider default (thinking on).

        Cycle 14: this replaces direct `_transport.post` access from rate_quality.
        Centralizes:
          - SSL context resolution
          - Timeout
          - Auth header
          - 5xx retry with exponential backoff (4xx not retried — request itself is bad)
          - Network-error retry (URLError that isn't HTTPError)
        Returns the raw `content` string from the first choice's message.

        Cycle 19 (defensive): when `retry_on_empty=True` (default) and the LLM
        returns 200-OK with empty `content` (DeepSeek-V4 reasoning ran out of
        tokens before emitting JSON), we retry ONCE with `max_tokens × 2`.
        This kills a class of bug we hit twice — Cycle 17 R1 (rate at 100) and
        Cycle 19 (rate at 400) — both times the cause was V4 reasoning headroom
        we hadn't budgeted for. Cost on the failure path doubles, which is
        acceptable since failures are rare. Caller can disable via
        `retry_on_empty=False` for paths where empty is meaningful.
        """
        import time as _t
        from urllib.error import HTTPError, URLError

        def _post(tokens: int) -> str:
            payload: dict[str, Any] = {
                "model": model or self.config.model,
                "messages": messages,
                "stream": False,
                "temperature": temperature,
                "max_tokens": tokens,
                "top_p": 0.9,
            }
            if response_format_json:
                payload["response_format"] = {"type": "json_object"}
            _apply_thinking(payload, self.config.provider, thinking)
            body_inner = json.dumps(payload).encode("utf-8")
            with self._transport.post(
                url, body_inner, headers, timeout=self.config.timeout_seconds, ssl_context=self.ssl_context
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
                return extract_message_content(raw)

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
                content = _post(max_tokens)
                # Cycle 19 defensive layer: empty content under V4 means reasoning
                # ate the budget. One double-or-nothing retry rescues most cases.
                if retry_on_empty and (not content or not content.strip()):
                    logger.info(
                        "general_chat got empty content at max_tokens=%d; retrying with %d",
                        max_tokens,
                        max_tokens * 2,
                    )
                    content = _post(max_tokens * 2)
                return content
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

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.5,
        thinking: str | None = None,
    ) -> Iterator[str]:
        """Stream raw content deltas for an arbitrary chat call (no JSON envelope).

        Cycle 23: transform modes stream plain prose, so unlike rewrite_stream
        there is no partial-JSON extraction — callers accumulate deltas as-is.
        No retry: streaming calls fail loudly and the caller maps to an SSE
        error event (same policy as rewrite_stream).
        `thinking` rides on _apply_thinking (None = provider default).
        """
        payload = {
            "model": model or self.config.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.9,
        }
        _apply_thinking(payload, self.config.provider, thinking)
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self.config.base_url}/chat/completions"
        with self._transport.post(
            url, body, headers, timeout=self.config.timeout_seconds, ssl_context=self.ssl_context
        ) as response:
            for delta, done in iter_streamed_deltas(response):
                if done:
                    return
                if delta:
                    yield delta

    def rate_quality(self, rewritten: str, target: VarietyPreset, *, model: str | None = None) -> dict[str, Any]:
        """Score 0-5 how natively `rewritten` reads in `target`. Returns {score, reason}.

        `model` (2026-08): optional one-call override — the quality sweep tool
        judges with the pro tier while interactive rating stays on flash."""
        metadata = PRESET_METADATA[target]
        # Cycle 14: routes through general_chat → free 5xx retry + clean error path.
        # Cycle 17: bumped max_tokens 100 → 400 because DeepSeek-V4 burns reasoning
        #           tokens before emitting JSON; 100 truncated the response to ''.
        # Cycle 19: bumped 400 → 800. Same bug class re-surfaced when rating LONGER
        #           rewrites (e.g. shanghai_mandarin_style of a 50-char input ≈ 150
        #           output chars): the model needs ~600 reasoning tokens for the
        #           harder dialects, so the JSON tail got truncated empty again.
        #           Lesson: V4 reasoning isn't a constant — it scales with input
        #           complexity. Pick max_tokens with comfortable headroom, not the
        #           "smallest that worked in one test".
        content = self.general_chat(
            [
                {
                    "role": "system",
                    "content": build_rate_system_prompt(
                        metadata,
                        near_identity_target=(target == VarietyPreset.STANDARD_PUTONGHUA),
                    ),
                },
                {"role": "user", "content": rewritten},
            ],
            max_tokens=800,
            temperature=0.2,
            thinking="low",  # judging nativeness wants light reasoning, bounded
            model=model,
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
        # Same 2026-08 bug class as rewrite(): full reasoning on a 600-token
        # budget → zero content → 503 (caught live by smoke). Explanation
        # wants light CoT, bounded.
        _apply_thinking(payload, self.config.provider, "low")
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
        self._config: LLMConfig | None
        if client is not None:
            self._config = client.config
            self._client: ChatCompletionsClient | None = client
        else:
            self._config = config if config is not None else load_llm_config()
            self._client = ChatCompletionsClient(self._config) if self._config else None
        self.quality_store = quality_store  # if None, ratings not persisted; else rate_quality records them

    @property
    def client(self) -> ChatCompletionsClient | None:
        """Underlying LLM client, shared with sibling services (transform) so the
        whole app keeps one transport / SSL context / config."""
        return self._client

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
        if self._client is None:
            raise RewriteError(NO_LLM_MESSAGE)

        # No heuristic fallback: it returned the original text with a dialect
        # banner glued on, which reads like a real rewrite but isn't one. An
        # honest error beats a fake result — same contract as explain/rate.
        result = self._client.rewrite(
            normalized,
            target,
            scenario=scenario,
            glossary_lines=glossary_lines,
        )
        self._log_request(target, started, degraded=result.degraded)
        return result

    def rate_quality(
        self,
        rewritten: str,
        target: VarietyPreset,
        *,
        scenario: Scenario | None = None,
    ) -> dict[str, Any]:
        """Service wrapper: rate the native-ness of `rewritten` in `target`.

        Cycle 17: replaced the awkward `record_for=(variety_value, scenario_value)`
        tuple — the caller had to pass both `target` and `target.value`. Now the
        caller passes the `scenario` enum directly; we derive the storage keys.
        If `scenario` is provided and a quality_store is attached, the score is
        recorded into the per-(variety, scenario) quality stats.
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
            )
        return result

    def rewrite_stream(
        self,
        text: str,
        target: VarietyPreset,
        scenario: Scenario = Scenario.FRIENDS_CASUAL,
        *,
        glossary_lines: list[str] | None = None,
    ) -> Iterator[tuple[str, str, bool, dict[str, Any] | None]]:
        """Stream chunks. Yields (delta_text, partial_text, is_done, meta).

        meta is None on chunks; on is_done=True it carries
        {"degraded": bool, "warning": str | None} matching non-streaming
        rewrite()'s contract (review fix P3). Caller should still check
        partial_text is non-empty at done (failure signal).

        Raises RewriteError if no LLM is configured or the stream fails —
        the heuristic fallback is gone, so callers surface a real error
        instead of a banner-prefixed copy of the user's own input.

        Review fix P2: threads glossary, mirroring rewrite(). Previously it
        was silently dropped on this (primary) path.
        """
        normalized = validate_text(text)
        started = time.perf_counter()
        if self._client is None:
            raise RewriteError(NO_LLM_MESSAGE)
        completed = False
        try:
            for delta, partial, done, meta in self._client.rewrite_stream(
                normalized,
                target,
                scenario=scenario,
                glossary_lines=glossary_lines,
            ):
                yield delta, partial, done, meta
                if done:
                    break
            completed = True
        finally:
            # Log on failure too — the old except-branch did, and losing the
            # latency of a request that died mid-stream is the worst time to.
            self._log_request(target, started, degraded=False, failed=not completed)

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

    def characterize(self, recipient: str, mood: str) -> dict[str, Any]:
        """Cycle 18 (Function 1): turn a 2-question wizard into a structured profile.

        Returns {suggested_scenario, register_hint, emotional_tone, glossary_suggestions[]}.
        suggested_scenario is validated against the Scenario enum and falls back to
        FRIENDS_CASUAL if the LLM hallucinates an unknown value (graceful — same
        contract as parse_scenario in presets.py).
        """
        from native_chinese_assistant.presets import Scenario

        if self._client is None:
            raise RewriteError("情境向导需要 LLM 服务。请先配置 LLM_API_KEY 后再试。")
        # Cycle 18 v2 (per A6): 120 → 240 char cap. Long enough for a real sentence
        # per field; short enough that the prompt stays compact.
        # Cycle 21 self-audit #3: strip control chars (NUL through US + DEL).
        # Newlines / tabs in these free-form fields reach the system prompt
        # and are a classic injection vector — fake "[NEW SYSTEM]:..." etc.
        recipient = _CTRL_CHAR_RE.sub("", recipient or "").strip()[:240]
        mood = _CTRL_CHAR_RE.sub("", mood or "").strip()[:240]
        if not recipient and not mood:
            raise ValueError("recipient and mood cannot both be empty")
        scenario_values = [s.value for s in Scenario]
        content = self._client.general_chat(
            [
                {"role": "system", "content": build_characterize_system_prompt(scenario_values)},
                {"role": "user", "content": build_characterize_user_prompt(recipient, mood)},
            ],
            max_tokens=500,  # Cycle 17 lesson: V4 needs reasoning headroom
            temperature=0.3,
            thinking="disabled",  # classification + glossary lookup — no CoT needed
        )
        if not content or not content.strip():
            raise RewriteError("LLM returned empty content for characterize.")
        try:
            parsed = _parse_llm_json(content)
        except (json.JSONDecodeError, RewriteError) as exc:
            raise RewriteError(f"Invalid characterize JSON: {exc}") from exc
        # Validate scenario; coerce unknown to FRIENDS_CASUAL
        suggested = str(parsed.get("suggested_scenario", "")).strip()
        if suggested not in scenario_values:
            suggested = Scenario.FRIENDS_CASUAL.value
        glossary = parsed.get("glossary_suggestions") or []
        if not isinstance(glossary, list):
            glossary = []
        glossary = [str(x).strip()[:80] for x in glossary if str(x).strip()][:5]
        return {
            "suggested_scenario": suggested,
            "register_hint": str(parsed.get("register_hint", "")).strip()[:120],
            "emotional_tone": str(parsed.get("emotional_tone", "")).strip()[:60],
            "glossary_suggestions": glossary,
        }

    def _log_request(
        self, target: VarietyPreset, started: float, *, degraded: bool, failed: bool = False
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        provider = self._config.provider if self._config else "none"
        logger.info(
            "rewrite_request target=%s latency_ms=%s degraded=%s failed=%s provider=%s",
            target.value,
            duration_ms,
            degraded,
            failed,
            provider,
        )
