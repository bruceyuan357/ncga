from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from native_chinese_assistant.presets import (
    PRESET_METADATA,
    SCENARIO_METADATA,
    Scenario,
    Script,
    VarietyPreset,
    parse_scenario,
    preset_options,
    scenario_options,
)
from native_chinese_assistant.rewrite import (
    MAX_INPUT_CHARS,
    ChatCompletionsClient,
    HeuristicRewriter,
    LLMConfig,
    RewriteError,
    RewriteService,
    _parse_llm_json,
    build_system_prompt,
    default_ca_bundle,
    extract_streamed_content,
    load_dotenv,
    load_llm_config,
    parse_variety,
    validate_text,
)
from native_chinese_assistant.web import (
    App,
    RateLimiter,
    _pid_alive,
    _port_in_use,
    _print_port_in_use_help,
    _read_pid_file,
    _wait_until_port_free,
    _write_pid_file,
    run_server,
    status_server,
    stop_server,
)

# Cycle 14: tests must not inherit production .env's NCGA_AUTH_TOKEN. We set it to
# empty string (not pop) because load_dotenv() uses os.environ.setdefault — popping
# would let .env re-inject, but setting to "" wins the setdefault check, and
# `_check_auth` treats empty string as auth-disabled.
os.environ["NCGA_AUTH_TOKEN"] = ""
os.environ["NCGA_DATA_KEY"] = ""  # tests use plaintext store; no leftover crypto state
# Dev machines may have an encrypted quality store at the default path
# (~/.local/share/ncga/quality.json, encrypted with the .env NCGA_DATA_KEY).
# Tests blank NCGA_DATA_KEY, so touching that store raises KeyMismatchError and
# fails the whole suite. Point XDG_DATA_HOME at a throwaway dir so the default
# store/keyfile paths stay hermetic. Individual tests still override as needed.
os.environ.setdefault("XDG_DATA_HOME", tempfile.mkdtemp(prefix="ncga-test-xdg-"))


def setUpModule():
    os.environ["NCGA_AUTH_TOKEN"] = ""
    os.environ["NCGA_DATA_KEY"] = ""


# ---------------- helpers ----------------


def call_app(
    app: App,
    method: str,
    path: str,
    body: bytes = b"",
    content_type: str = "application/json",
    extra_environ: dict | None = None,
) -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": content_type,
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.input": io.BytesIO(body),
    }
    if extra_environ:
        environ.update(extra_environ)
    response = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], response


class FakeStreamResponse:
    """Minimal context manager mimicking urllib's HTTPResponse for a non-streaming call."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        # Iterating yields one "line" per newline-separated chunk — used by streaming clients.
        for line in self._body.split(b"\n"):
            if line:
                yield line + b"\n"


class StreamingFakeResponse(FakeStreamResponse):
    """Iterates over pre-split lines instead of splitting by \\n."""

    def __init__(self, lines: list[bytes]) -> None:
        super().__init__(b"".join(lines))
        self._lines = lines

    def __iter__(self):
        yield from self._lines


class FakeTransport:
    """In-memory transport injected into ChatCompletionsClient."""

    def __init__(self, response_body: bytes) -> None:
        self.response_body = response_body
        self.calls: list[dict] = []

    def post(self, url, body, headers, *, timeout, ssl_context):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        return FakeStreamResponse(self.response_body)


def _llm_json(rewritten: str, warning: str = "") -> bytes:
    payload = {
        "choices": [{"message": {"content": json.dumps({"rewritten_text": rewritten, "warning": warning})}}]
    }
    return json.dumps(payload).encode("utf-8")


def _llm_json_raw(content: str) -> bytes:
    """Wrap an arbitrary content string in the chat-completions response shape.
    Used by Cycle 18 tests where the LLM is supposed to return characterize JSON,
    not the rewrite envelope."""
    payload = {"choices": [{"message": {"content": content}}]}
    return json.dumps(payload).encode("utf-8")


def _build_client(
    response_body: bytes, *, streaming: bool = False
) -> tuple[ChatCompletionsClient, FakeTransport]:
    config = LLMConfig(
        provider="deepseek",
        api_key="test",
        model="test-model",
        base_url="https://test.example",
        streaming=streaming,
        ca_bundle=None,
        skip_ssl_verify=False,
        timeout_seconds=5.0,
    )
    transport = FakeTransport(response_body)
    return ChatCompletionsClient(config, transport=transport), transport


# ---------------- validation ----------------


class RewriteValidationTests(unittest.TestCase):
    def test_validate_text_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_text("   \n")

    def test_validate_text_rejects_too_long(self) -> None:
        with self.assertRaises(ValueError):
            validate_text("字" * (MAX_INPUT_CHARS + 1))

    def test_parse_variety_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            parse_variety("mars_mandarin")


# ---------------- prompt construction ----------------


class PromptBuildingTests(unittest.TestCase):
    def test_build_system_prompt_does_not_choke_on_curly_braces(self) -> None:
        # If presets.style_notes ever contains literal `{` `}` (e.g. example JSON),
        # f-string composition must not fail.
        meta = PRESET_METADATA[VarietyPreset.STANDARD_PUTONGHUA]
        prompt = build_system_prompt(meta)
        self.assertIn(meta.label, prompt)
        self.assertIn("rewritten_text", prompt)

    def test_build_system_prompt_strict_json_mode_adds_warning(self) -> None:
        meta = PRESET_METADATA[VarietyPreset.STANDARD_PUTONGHUA]
        prompt = build_system_prompt(meta, strict_json=True)
        self.assertIn("纯 JSON", prompt)

    def test_build_system_prompt_uses_default_scenario(self) -> None:
        meta = PRESET_METADATA[VarietyPreset.STANDARD_PUTONGHUA]
        prompt = build_system_prompt(meta)
        # Default scenario is FRIENDS_CASUAL — its addendum should appear.
        self.assertIn(SCENARIO_METADATA[Scenario.FRIENDS_CASUAL].prompt_addendum[:8], prompt)

    def test_build_system_prompt_changes_per_scenario(self) -> None:
        meta = PRESET_METADATA[VarietyPreset.BEIJING_MANDARIN]
        casual = build_system_prompt(meta, scenario=Scenario.FRIENDS_CASUAL)
        elders = build_system_prompt(meta, scenario=Scenario.WITH_ELDERS)
        workplace = build_system_prompt(meta, scenario=Scenario.WORKPLACE)
        self.assertNotEqual(casual, elders)
        self.assertNotEqual(casual, workplace)
        # Elders prompt should mention long-form respect markers
        self.assertIn("长辈", elders)
        # Workplace should mention work
        self.assertIn("工作", workplace)

    def test_build_system_prompt_declares_multilanguage_input(self) -> None:
        # Cycle 20: user may type in any language; output must remain native
        # Chinese in the target variety, with consistency across input languages.
        meta = PRESET_METADATA[VarietyPreset.BEIJING_MANDARIN]
        prompt = build_system_prompt(meta)
        self.assertIn("英文", prompt)
        self.assertIn("输出语言锁定", prompt)
        self.assertIn("跨语言一致性", prompt)
        self.assertIn("双语对照", prompt)

    def test_build_user_prompt_signals_input_language_unrestricted(self) -> None:
        from native_chinese_assistant.rewrite import build_user_prompt

        out = build_user_prompt("Hello, how are you?", VarietyPreset.SHANGHAI_MANDARIN_STYLE)
        self.assertIn("输入语言不限", out)
        self.assertIn("Hello, how are you?", out)


class MultiLanguageInputTests(unittest.TestCase):
    """Cycle 20: non-Chinese input flows through the rewrite path correctly.
    Transport is mocked; we verify:
      1. input text reaches the LLM verbatim
      2. system prompt carries the multi-language rule
      3. parse + return contract is identical to Chinese-input case
    """

    def test_english_input_reaches_llm_and_parses(self) -> None:
        client, transport = _build_client(_llm_json("嗨喽,今儿过得咋样啊?"))
        result = client.rewrite("Hello, how's your day going?", VarietyPreset.BEIJING_MANDARIN)
        self.assertEqual(result.rewritten_text, "嗨喽,今儿过得咋样啊?")
        self.assertFalse(result.degraded)
        body = json.loads(transport.calls[0]["body"])
        user_msg = next(m for m in body["messages"] if m["role"] == "user")
        self.assertIn("Hello, how's your day going?", user_msg["content"])
        sys_msg = next(m for m in body["messages"] if m["role"] == "system")
        self.assertIn("输出语言锁定", sys_msg["content"])

    def test_japanese_input_reaches_llm_and_parses(self) -> None:
        client, transport = _build_client(_llm_json("侬早呀。"))
        result = client.rewrite("おはようございます", VarietyPreset.SHANGHAI_MANDARIN_STYLE)
        self.assertEqual(result.rewritten_text, "侬早呀。")
        body = json.loads(transport.calls[0]["body"])
        user_msg = next(m for m in body["messages"] if m["role"] == "user")
        self.assertIn("おはようございます", user_msg["content"])

    def test_heuristic_refuses_non_chinese_input(self) -> None:
        # Cycle 20: prepending a Chinese banner to English ("给你整成...:very
        # good") is worse than admitting offline mode can't help.
        from native_chinese_assistant.rewrite import HeuristicRewriter

        result = HeuristicRewriter().rewrite("This is a very good day.", VarietyPreset.DONGBEI_MANDARIN)
        self.assertEqual(result.rewritten_text, "This is a very good day.")
        self.assertTrue(result.degraded)
        self.assertIn("非中文", result.warning or "")
        self.assertNotIn("给你", result.rewritten_text)


class ScenarioParsingTests(unittest.TestCase):
    def test_parse_scenario_default_for_none(self) -> None:
        self.assertEqual(parse_scenario(None), Scenario.FRIENDS_CASUAL)

    def test_parse_scenario_default_for_empty(self) -> None:
        self.assertEqual(parse_scenario(""), Scenario.FRIENDS_CASUAL)

    def test_parse_scenario_default_for_unknown(self) -> None:
        # Unknown values silently fall back rather than raise.
        self.assertEqual(parse_scenario("mars_chat"), Scenario.FRIENDS_CASUAL)

    def test_parse_scenario_known(self) -> None:
        self.assertEqual(parse_scenario("with_elders"), Scenario.WITH_ELDERS)
        self.assertEqual(parse_scenario("workplace"), Scenario.WORKPLACE)
        self.assertEqual(parse_scenario("venting"), Scenario.VENTING)

    def test_scenario_options_shape(self) -> None:
        opts = scenario_options()
        self.assertEqual(len(opts), len(Scenario))
        for o in opts:
            self.assertIn("value", o)
            self.assertIn("label", o)
            self.assertIn("description", o)


# ---------------- JSON parsing ----------------


class LLMJsonParseTests(unittest.TestCase):
    def test_parse_strict_json(self) -> None:
        self.assertEqual(_parse_llm_json('{"rewritten_text": "你好"}'), {"rewritten_text": "你好"})

    def test_parse_strips_code_fence(self) -> None:
        raw = '```json\n{"rewritten_text": "你好"}\n```'
        self.assertEqual(_parse_llm_json(raw), {"rewritten_text": "你好"})

    def test_parse_salvages_object_from_preamble(self) -> None:
        raw = 'Sure, here is the result: {"rewritten_text": "你好"} hope it helps.'
        self.assertEqual(_parse_llm_json(raw), {"rewritten_text": "你好"})

    def test_parse_rejects_non_object(self) -> None:
        with self.assertRaises(RewriteError):
            _parse_llm_json('"just a string"')


# ---------------- LLM client (mocked) ----------------


class ChatCompletionsClientTests(unittest.TestCase):
    def test_non_streaming_happy_path(self) -> None:
        client, transport = _build_client(_llm_json("你好啊"))
        result = client.rewrite("你好", VarietyPreset.STANDARD_PUTONGHUA)
        self.assertEqual(result.rewritten_text, "你好啊")
        self.assertFalse(result.degraded)
        self.assertEqual(len(transport.calls), 1)

    def test_truncates_oversized_output(self) -> None:
        from native_chinese_assistant.rewrite import MAX_OUTPUT_CHARS

        oversized = "字" * (MAX_OUTPUT_CHARS + 50)
        client, _ = _build_client(_llm_json(oversized))
        result = client.rewrite("原文", VarietyPreset.BEIJING_MANDARIN)
        self.assertEqual(len(result.rewritten_text), MAX_OUTPUT_CHARS)
        self.assertIn("truncated", result.warning or "")

    def test_retries_on_invalid_json_then_recovers(self) -> None:
        # First call returns garbage, second call returns valid JSON.
        good = json.dumps({"rewritten_text": "好的", "warning": ""})
        bad = "this is not json at all"
        responses = [
            json.dumps({"choices": [{"message": {"content": bad}}]}).encode("utf-8"),
            json.dumps({"choices": [{"message": {"content": good}}]}).encode("utf-8"),
        ]

        client, _ = _build_client(responses[0])
        # Patch transport to flip on second call
        call_idx = {"i": 0}
        original_post = client._transport.post

        def flaky_post(*args, **kwargs):
            i = call_idx["i"]
            call_idx["i"] += 1
            client._transport.response_body = responses[i] if i < len(responses) else responses[-1]
            return original_post(*args, **kwargs)

        client._transport.post = flaky_post
        result = client.rewrite("文", VarietyPreset.DONGBEI_MANDARIN)
        self.assertEqual(result.rewritten_text, "好的")
        self.assertEqual(call_idx["i"], 2)

    def test_raises_after_retries_exhausted(self) -> None:
        bad = json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode("utf-8")
        client, _ = _build_client(bad)
        with self.assertRaises(RewriteError):
            client.rewrite("文", VarietyPreset.DONGBEI_MANDARIN)

    def test_empty_stream_retries_non_streaming(self) -> None:
        """Transient empty SSE streams (observed on deepseek-v4-flash under
        parallel load) must fall back to a plain non-streaming request instead
        of repeating the identical flaky streaming call."""
        empty_sse = b"data: [DONE]\n\n"
        ok = _llm_json("非流式兜底成功")
        client, transport = _build_client(ok, streaming=True)
        original_post = transport.post

        def flaky_post(url, body, headers, *, timeout, ssl_context):
            payload = json.loads(body.decode("utf-8"))
            if payload.get("stream"):
                transport.calls.append(
                    {"url": url, "body": body, "headers": headers, "timeout": timeout}
                )
                return FakeStreamResponse(empty_sse)
            return original_post(url, body, headers, timeout=timeout, ssl_context=ssl_context)

        transport.post = flaky_post
        result = client.rewrite("你好", VarietyPreset.BEIJING_MANDARIN)
        self.assertEqual(result.rewritten_text, "非流式兜底成功")
        self.assertFalse(result.degraded)
        streams = [json.loads(c["body"].decode("utf-8"))["stream"] for c in transport.calls]
        self.assertEqual(streams, [True, False])

    def test_persistent_empty_stream_still_raises_after_bonus_retry(self) -> None:
        """If the non-streaming fallback also fails, the error propagates after
        one bonus attempt (3 calls total) — heuristic degradation takes over."""
        empty_sse = b"data: [DONE]\n\n"
        client, transport = _build_client(empty_sse, streaming=True)
        with self.assertRaises(RewriteError):
            client.rewrite("你好", VarietyPreset.BEIJING_MANDARIN)
        self.assertEqual(len(transport.calls), 3)


# ---------------- DeepSeek thinking (reasoning) control ----------------


class ThinkingControlTests(unittest.TestCase):
    """2026-08 root-cause fix: deepseek-v4-flash reasoning ate the max_tokens
    budget and returned zero content. Per-call thinking control is the fix —
    these tests pin the payload shape per provider."""

    @staticmethod
    def _last_payload(transport) -> dict:
        return json.loads(transport.calls[-1]["body"].decode("utf-8"))

    def test_rewrite_payload_disables_thinking_on_deepseek(self) -> None:
        client, transport = _build_client(_llm_json("ok"))
        client.rewrite("文", VarietyPreset.BEIJING_MANDARIN)
        payload = self._last_payload(transport)
        self.assertEqual(payload.get("thinking"), {"type": "disabled"})

    def test_general_chat_thinking_disabled(self) -> None:
        client, transport = _build_client(_llm_json_raw('{"ok": true}'))
        client.general_chat([{"role": "user", "content": "hi"}], thinking="disabled")
        payload = self._last_payload(transport)
        self.assertEqual(payload.get("thinking"), {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)

    def test_general_chat_thinking_low(self) -> None:
        client, transport = _build_client(_llm_json_raw('{"ok": true}'))
        client.general_chat([{"role": "user", "content": "hi"}], thinking="low")
        payload = self._last_payload(transport)
        self.assertEqual(payload.get("reasoning_effort"), "low")
        self.assertNotIn("thinking", payload)

    def test_general_chat_thinking_default_sends_nothing(self) -> None:
        client, transport = _build_client(_llm_json_raw('{"ok": true}'))
        client.general_chat([{"role": "user", "content": "hi"}])
        payload = self._last_payload(transport)
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_thinking_never_sent_to_openai(self) -> None:
        client, transport = _build_client(_llm_json_raw('{"ok": true}'))
        client.config = client.config.__class__(**{**client.config.__dict__, "provider": "openai"})
        client.general_chat([{"role": "user", "content": "hi"}], thinking="disabled")
        payload = self._last_payload(transport)
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_unknown_thinking_mode_rejected(self) -> None:
        from native_chinese_assistant.rewrite import _apply_thinking

        with self.assertRaises(ValueError):
            _apply_thinking({}, "deepseek", "maximum")

    def test_hard_variety_routes_to_pro_model(self) -> None:
        """2026-08 experiment: pro tier is the quality lever for low-resource
        varieties. hokkien/minnan/cantonese/shanghai/jianghuai → pro; the
        Mandarin family stays on the configured (flash) model."""
        client, transport = _build_client(_llm_json("好"))
        client.rewrite("好", VarietyPreset.HOKKIEN_WRITTEN)
        payload = self._last_payload(transport)
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        client.rewrite("好", VarietyPreset.BEIJING_MANDARIN)
        payload = self._last_payload(transport)
        self.assertEqual(payload["model"], "test-model")

    @staticmethod
    def _system_content(transport):
        body = json.loads(transport.calls[0]["body"].decode("utf-8"))
        return body["messages"][0]["content"]

    def test_scenario_appears_in_system_prompt_payload(self) -> None:
        """Smoke test: when scenario=WORKPLACE is passed, the system prompt sent to the
        LLM transport must include the workplace addendum."""
        client, transport = _build_client(_llm_json("ok"))
        client.rewrite("你好", VarietyPreset.STANDARD_PUTONGHUA, scenario=Scenario.WORKPLACE)
        self.assertIn("工作沟通", self._system_content(transport))

    def test_default_scenario_is_friends_casual(self) -> None:
        client, transport = _build_client(_llm_json("ok"))
        client.rewrite("你好", VarietyPreset.STANDARD_PUTONGHUA)  # no scenario kw
        # Friends casual addendum mentions 同辈
        self.assertIn("同辈", self._system_content(transport))


# ---------------- service ----------------


class RewriteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_heuristic_rewriter_returns_script_and_warning(self) -> None:
        result = HeuristicRewriter().rewrite("这个朋友真的非常不错。", VarietyPreset.BEIJING_MANDARIN)
        self.assertEqual(result.script, Script.SIMPLIFIED)
        self.assertIn("warning", result.as_dict())
        self.assertTrue(result.rewritten_text)
        self.assertTrue(result.degraded)

    def test_service_falls_back_without_llm(self) -> None:
        for key in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_MODEL", "LLM_PROVIDER"):
            os.environ.pop(key, None)
        service = RewriteService(config=None)
        service._client = None  # force fallback even if dotenv re-injected
        result = service.rewrite("我们一起去吃饭吧。", VarietyPreset.CANTONESE_WRITTEN)
        self.assertEqual(result.target_variety, VarietyPreset.CANTONESE_WRITTEN)
        self.assertEqual(result.script, Script.TRADITIONAL)
        self.assertTrue(result.warning)
        self.assertTrue(result.degraded)

    def test_service_uses_injected_client(self) -> None:
        client, _ = _build_client(_llm_json("好嘅"))
        service = RewriteService(client=client)
        result = service.rewrite("好的", VarietyPreset.CANTONESE_WRITTEN)
        self.assertEqual(result.rewritten_text, "好嘅")
        self.assertFalse(result.degraded)

    def test_service_falls_back_when_client_raises(self) -> None:
        bad = json.dumps({"choices": [{"message": {"content": "garbage"}}]}).encode("utf-8")
        client, _ = _build_client(bad)
        service = RewriteService(client=client)
        with self.assertLogs("ncga.rewrite", level="WARNING"):
            result = service.rewrite("好的", VarietyPreset.CANTONESE_WRITTEN)
        self.assertTrue(result.degraded)
        self.assertIn("heuristic", result.warning or "")

    def test_load_dotenv_sets_missing_values(self) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False) as handle:
            handle.write("LLM_PROVIDER=deepseek\nLLM_MODEL=deepseek-chat\nLLM_STREAM=true\n")
            path = handle.name
        try:
            for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_STREAM"):
                os.environ.pop(k, None)
            load_dotenv(path)
            self.assertEqual(os.environ["LLM_PROVIDER"], "deepseek")
            self.assertEqual(os.environ["LLM_MODEL"], "deepseek-chat")
            self.assertEqual(os.environ["LLM_STREAM"], "true")
        finally:
            os.unlink(path)

    def test_load_llm_config_prefers_deepseek_defaults(self) -> None:
        os.environ.pop("LLM_MODEL", None)
        os.environ["LLM_PROVIDER"] = "deepseek"
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        config = load_llm_config()
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.provider, "deepseek")
        self.assertTrue(config.model)
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertTrue(config.streaming)
        self.assertEqual(config.ca_bundle, default_ca_bundle())
        self.assertGreater(config.timeout_seconds, 0)

    def test_load_llm_config_returns_none_without_key(self) -> None:
        for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(k, None)
        # Bypass .env so the test is hermetic regardless of local DEEPSEEK_API_KEY.
        with mock.patch("native_chinese_assistant.rewrite.load_dotenv", lambda *a, **kw: None):
            self.assertIsNone(load_llm_config())

    def test_extract_streamed_content_joins_sse_chunks(self) -> None:
        lines = [
            b'data: {"choices":[{"delta":{"content":"{\\"rewritten_text\\": \\""}}]}\n',
            b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd\\""}}]}\n',
            b'data: {"choices":[{"delta":{"content":", \\"warning\\": \\"\\"}"}}]}\n',
            b"data: [DONE]\n",
        ]
        self.assertEqual(extract_streamed_content(lines), '{"rewritten_text": "你好", "warning": ""}')

    def test_extract_streamed_content_wraps_unicode_decode_error(self) -> None:
        """Cycle 21 self-audit #4: a network blip mid-stream can deliver
        bytes that don't form valid UTF-8 (truncated multibyte sequence).
        Used to leak UnicodeDecodeError past the caller (which only catches
        RewriteError). Now wrapped."""
        lines = [
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n',
            b"\xff\xfe\xfd\n",  # garbage bytes mid-stream
            b"data: [DONE]\n",
        ]
        with self.assertRaises(RewriteError) as ctx:
            extract_streamed_content(lines)
        self.assertIn("UTF-8", str(ctx.exception))

    def test_iter_streamed_deltas_wraps_unicode_decode_error(self) -> None:
        """Same UTF-8 guard on the streaming generator path."""
        from native_chinese_assistant.rewrite import iter_streamed_deltas

        lines = [
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n',
            b"\xff\xfe\xfd\n",
            b"data: [DONE]\n",
        ]
        gen = iter_streamed_deltas(lines)
        # First yield is the legitimate "x" chunk.
        delta1, done1 = next(gen)
        self.assertEqual((delta1, done1), ("x", False))
        # Next iteration hits the garbage bytes and must raise RewriteError,
        # not UnicodeDecodeError.
        with self.assertRaises(RewriteError) as ctx:
            next(gen)
        self.assertIn("UTF-8", str(ctx.exception))

    def test_heuristic_rewriter_includes_real_failure_reason(self) -> None:
        result = HeuristicRewriter().rewrite(
            "你好", VarietyPreset.BEIJING_MANDARIN, reason="LLM request failed."
        )
        self.assertIn("LLM request failed.", result.warning)


# ---------------- preset options shape ----------------


class PresetOptionsTests(unittest.TestCase):
    def test_options_include_full_metadata(self) -> None:
        options = preset_options()
        self.assertEqual(len(options), len(PRESET_METADATA))
        for opt in options:
            for key in (
                "value",
                "label",
                "script",
                "letter",
                "trial",
                "tts_lang",
                "description_short",
                "description_style",
                "keywords",
                "landmarks",
            ):
                self.assertIn(key, opt)
            self.assertIsInstance(opt["keywords"], list)
            self.assertIsInstance(opt["landmarks"], list)
            for lm in opt["landmarks"]:
                self.assertIn("url", lm)
                self.assertIn("name", lm)


# ---------------- web app ----------------


class WebAppTests(unittest.TestCase):
    def _make_app(self) -> App:
        client, _ = _build_client(_llm_json("好的（mocked）"))
        return App(rewrite_service=RewriteService(client=client))

    def test_presets_endpoint_returns_options(self) -> None:
        status, headers, body = call_app(self._make_app(), "GET", "/api/presets")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        self.assertTrue(payload["presets"])
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_healthz_endpoint(self) -> None:
        status, _, body = call_app(self._make_app(), "GET", "/api/healthz")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_rewrite_endpoint_rejects_empty_text(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": " ", "target_variety": VarietyPreset.STANDARD_PUTONGHUA.value}).encode(
                "utf-8"
            ),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Text must not be empty", payload["error"])

    def test_rewrite_endpoint_rejects_unknown_preset(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "你好", "target_variety": "unknown"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Unsupported target variety", payload["error"])

    def test_rewrite_endpoint_returns_structured_payload(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps(
                {"text": "这个安排非常不错。", "target_variety": VarietyPreset.DONGBEI_MANDARIN.value}
            ).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["target_variety"], VarietyPreset.DONGBEI_MANDARIN.value)
        self.assertEqual(payload["script"], Script.SIMPLIFIED.value)
        self.assertEqual(payload["rewritten_text"], "好的（mocked）")
        self.assertFalse(payload["degraded"])

    def test_rewrite_endpoint_rejects_oversized_body(self) -> None:
        app = App(rewrite_service=RewriteService(config=None), max_body_bytes=64)
        big = b'{"text":"' + (b"x" * 100) + b'","target_variety":"standard_putonghua"}'
        status, _, _ = call_app(app, "POST", "/api/rewrite", body=big)
        self.assertEqual(status, "413 Payload Too Large")

    def test_rewrite_endpoint_rejects_string_text_overflow(self) -> None:
        # With the 64K default body cap, a payload of MAX_INPUT_CHARS*4 chars now passes
        # the body check and hits the per-text size validator → 400.
        # Use a tiny body cap to test the body-cap path explicitly.
        app = App(rewrite_service=RewriteService(config=None), max_body_bytes=4096)
        body = json.dumps(
            {"text": "字" * (MAX_INPUT_CHARS * 4 + 1), "target_variety": "standard_putonghua"}
        ).encode("utf-8")
        status, _, _ = call_app(app, "POST", "/api/rewrite", body=body)
        self.assertEqual(status, "413 Payload Too Large")

    def test_static_blocks_path_traversal(self) -> None:
        app = self._make_app()
        status, _, body = call_app(app, "GET", "/static/../app.py")
        self.assertEqual(status, "404 Not Found")

    def test_scenarios_endpoint(self) -> None:
        status, _, body = call_app(self._make_app(), "GET", "/api/scenarios")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(payload["scenarios"]), len(Scenario))
        values = {s["value"] for s in payload["scenarios"]}
        self.assertIn("friends_casual", values)
        self.assertIn("workplace", values)

    @staticmethod
    def _system_prompt(transport):
        body = json.loads(transport.calls[0]["body"].decode("utf-8"))
        return body["messages"][0]["content"]

    def test_rewrite_endpoint_accepts_scenario(self) -> None:
        client, transport = _build_client(_llm_json("好的（场景测试）"))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps(
                {
                    "text": "你好",
                    "target_variety": "beijing_mandarin",
                    "scenario": "with_elders",
                }
            ).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        # Verify the addendum for WITH_ELDERS made it into the LLM payload
        self.assertIn("长辈", self._system_prompt(transport))

    def test_explain_endpoint_happy_path(self) -> None:
        # Server's first call is /api/explain, which uses non-streaming JSON.
        explain_resp = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "整体往北京味儿走",
                                    "points": [
                                        {"from": "非常", "to": "特", "why": "京片子习惯"},
                                        {"from": "朋友", "to": "哥们儿", "why": "胡同儿话"},
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        client, transport = _build_client(explain_resp)
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/explain",
            body=json.dumps(
                {
                    "original": "我朋友非常厉害",
                    "rewritten": "我哥们儿特厉害",
                    "target_variety": "beijing_mandarin",
                }
            ).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(len(payload["points"]), 2)
        self.assertEqual(payload["points"][0]["from"], "非常")
        self.assertEqual(payload["points"][0]["to"], "特")
        self.assertIn("北京", payload["summary"])
        # Verify the system prompt included the variety label
        sent = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertIn("北京话", sent["messages"][0]["content"])

    def test_explain_endpoint_falls_back_to_markdown_on_garbled_json(self) -> None:
        """If the LLM returns non-JSON prose, surface it as markdown rather than 400."""
        garbled = json.dumps(
            {"choices": [{"message": {"content": "Sorry I can't return JSON. Just FYI: 朋友→哥们儿"}}]}
        ).encode("utf-8")
        client, _ = _build_client(garbled)
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/explain",
            body=json.dumps(
                {"original": "我朋友厉害", "rewritten": "我哥们儿厉害", "target_variety": "beijing_mandarin"}
            ).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("markdown", payload)
        self.assertIn("朋友", payload["markdown"])

    def test_explain_endpoint_503_when_llm_returns_empty(self) -> None:
        """LLM occasionally returns empty content. We surface a 503 rather than confuse the user with 400."""
        empty = json.dumps({"choices": [{"message": {"content": ""}}]}).encode("utf-8")
        client, _ = _build_client(empty)
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/explain",
            body=json.dumps(
                {"original": "hi", "rewritten": "hey", "target_variety": "beijing_mandarin"}
            ).encode("utf-8"),
        )
        self.assertEqual(status, "503 Service Unavailable")

    def test_explain_endpoint_503_without_llm(self) -> None:
        """Without an LLM client, explain must 503 (heuristic can't explain)."""
        # Bypass .env so the result doesn't depend on the local DEEPSEEK_API_KEY.
        with mock.patch("native_chinese_assistant.rewrite.load_llm_config", return_value=None):
            service = RewriteService(config=None)
        self.assertIsNone(service._client)
        app = App(rewrite_service=service)
        status, _, body = call_app(
            app,
            "POST",
            "/api/explain",
            body=json.dumps(
                {
                    "original": "你好",
                    "rewritten": "你好啊",
                    "target_variety": "standard_putonghua",
                }
            ).encode("utf-8"),
        )
        self.assertEqual(status, "503 Service Unavailable")
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("LLM", payload["error"])

    def test_explain_endpoint_rejects_missing_fields(self) -> None:
        app = App(rewrite_service=RewriteService(config=None))
        status, _, _ = call_app(
            app,
            "POST",
            "/api/explain",
            body=json.dumps({"original": "你好"}).encode("utf-8"),
        )
        self.assertEqual(status, "400 Bad Request")

    def test_rewrite_endpoint_unknown_scenario_falls_back_silently(self) -> None:
        """Unknown scenario must not 400 — it silently falls back to FRIENDS_CASUAL.

        Cycle 16: the fallback is now visible in the response body via
        `effective_scenario`, so a stale-frontend / typo'd scenario doesn't
        silently misroute output without leaving a trace the caller can inspect.
        """
        client, transport = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps(
                {"text": "你好", "target_variety": "beijing_mandarin", "scenario": "mars_chat"}
            ).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        # Friends casual addendum
        self.assertIn("同辈", self._system_prompt(transport))
        # The chosen scenario is echoed so the caller can detect the silent fallback.
        self.assertEqual(payload["effective_scenario"], Scenario.FRIENDS_CASUAL.value)

    def test_security_headers_present_on_static(self) -> None:
        app = self._make_app()
        status, headers, _ = call_app(app, "GET", "/")
        self.assertEqual(status, "200 OK")
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers["X-Frame-Options"], "DENY")


# ---------------- Cycle 20: in-app reflection form (POST /api/feedback) ----------------


class FeedbackEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ncga-fb-jsonl-"))
        self.store_path = self.tmpdir / "feedback.jsonl"
        os.environ["NCGA_FEEDBACK_STORE"] = str(self.store_path)

    def tearDown(self) -> None:
        import shutil

        os.environ.pop("NCGA_FEEDBACK_STORE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_app(self) -> App:
        return App(rewrite_service=RewriteService(config=None))

    def _post(self, app: App, body_obj: dict):
        return call_app(app, "POST", "/api/feedback", body=json.dumps(body_obj).encode("utf-8"))

    def test_happy_path_persists_jsonl_line(self) -> None:
        app = self._make_app()
        status, _, body = self._post(
            app,
            {
                "rating": 5,
                "liked": ["方言味地道", "速度快"],
                "wishlist": ["UI"],
                "note": "试用 30 分钟体验很好",
                "contact": "wechat: alice",
                "variety": "beijing_mandarin",
                "scenario": "friends_casual",
                "input_language": "en",
            },
        )
        self.assertEqual(status, "200 OK")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["id"].startswith("fb_"))
        self.assertTrue(self.store_path.exists())
        lines = self.store_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["rating"], 5)
        self.assertEqual(record["liked"], ["方言味地道", "速度快"])
        self.assertEqual(record["variety"], "beijing_mandarin")
        self.assertNotIn("127.0.0.1", lines[0])  # ip is hashed
        self.assertEqual(len(record["ip_hash"]), 12)
        self.assertTrue(record["ts"].endswith("Z"))

    def test_file_perm_0o600(self) -> None:
        # Cycle 21 self-audit #1: file must be 0o600 not 0o644.
        app = self._make_app()
        self._post(app, {"rating": 3})
        mode = os.stat(self.store_path).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    def test_rating_required(self) -> None:
        status, _, body = self._post(self._make_app(), {"note": "no rating"})
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("rating", json.loads(body)["error"])

    def test_rating_out_of_range(self) -> None:
        status, _, body = self._post(self._make_app(), {"rating": 7})
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("1", json.loads(body)["error"])
        status, _, _ = self._post(self._make_app(), {"rating": 0})
        self.assertEqual(status, "400 Bad Request")

    def test_rating_must_be_integer(self) -> None:
        status, _, body = self._post(self._make_app(), {"rating": "five"})
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("integer", json.loads(body)["error"])

    def test_chips_must_be_arrays(self) -> None:
        status, _, body = self._post(self._make_app(), {"rating": 3, "liked": "not-an-array"})
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("arrays", json.loads(body)["error"])

    def test_note_truncates_at_cap(self) -> None:
        long_note = "x" * 2000
        status, _, _ = self._post(self._make_app(), {"rating": 3, "note": long_note})
        self.assertEqual(status, "200 OK")
        record = json.loads(self.store_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(len(record["note"]), 800)

    def test_chip_count_and_length_capped(self) -> None:
        chips = ["a" * 50] * 20
        status, _, _ = self._post(self._make_app(), {"rating": 4, "liked": chips})
        self.assertEqual(status, "200 OK")
        record = json.loads(self.store_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(len(record["liked"]), 10)
        self.assertEqual(len(record["liked"][0]), 24)

    def test_control_chars_stripped_from_note(self) -> None:
        # Cycle 20: terminal-injection defense. \r\n + ANSI escape in a note
        # would otherwise let an attacker mess with the operator's `tail`.
        status, _, _ = self._post(
            self._make_app(),
            {"rating": 4, "note": "hello\n\rworld\x1b[31mRED\x1b[0m\x7f"},
        )
        self.assertEqual(status, "200 OK")
        record = json.loads(self.store_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["note"], "helloworld[31mRED[0m")

    def test_rate_limit_blocks_burst(self) -> None:
        app = self._make_app()
        for _ in range(5):
            status, _, _ = self._post(app, {"rating": 3})
            self.assertEqual(status, "200 OK")
        status, _, body = self._post(app, {"rating": 3})
        self.assertEqual(status, "429 Too Many Requests")
        self.assertIn("反馈", json.loads(body)["error"])


# ---------------- Cycle 16: validation diagnostics ----------------
#
# Each user-correctable 4xx must carry a precise, actionable string (RFC 7807 spirit).
# These tests lock that in so the regression of "Invalid JSON payload" cannot recur.


class ValidationDiagnosticsTests(unittest.TestCase):
    def _make_app(self) -> App:
        client, _ = _build_client(_llm_json("好的（mocked）"))
        return App(rewrite_service=RewriteService(client=client))

    def test_rewrite_missing_text_field_names_the_field(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps({"target_variety": "beijing_mandarin"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Missing required field", payload["error"])
        self.assertIn("text", payload["error"])

    def test_rewrite_missing_target_variety_field_names_the_field(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "你好"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Missing required field", payload["error"])
        self.assertIn("target_variety", payload["error"])

    def test_rewrite_malformed_json_reports_parse_location(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body='{"text": "你好",,,, broken'.encode(),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Malformed JSON", payload["error"])
        # the parser surfaces a line/column so the caller can find the typo
        self.assertIn("line", payload["error"])

    def test_rewrite_non_dict_body_rejects_with_object_message(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=b"[1, 2, 3]",
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("JSON object", payload["error"])

    def test_rewrite_invalid_utf8_body_reports_utf8(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=b"\xff\xfe\xff",  # not valid UTF-8
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("UTF-8", payload["error"])

    def test_rewrite_unknown_variety_echoes_offending_value(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "你好", "target_variety": "klingon_mandarin"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Unsupported target variety", payload["error"])
        self.assertIn("klingon_mandarin", payload["error"])

    def test_rewrite_response_includes_effective_scenario(self) -> None:
        """A successful /api/rewrite response always names the scenario actually applied."""
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rewrite",
            body=json.dumps(
                {
                    "text": "你好",
                    "target_variety": "beijing_mandarin",
                    "scenario": "workplace",
                }
            ).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["effective_scenario"], "workplace")

    def test_rate_endpoint_missing_field_names_it(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/rate",
            body=json.dumps({"target_variety": "beijing_mandarin"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Missing required field", payload["error"])
        self.assertIn("rewritten", payload["error"])

    def test_explain_endpoint_missing_field_names_it(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/explain",
            body=json.dumps({"original": "hi", "rewritten": "hi"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Missing required field", payload["error"])
        self.assertIn("target_variety", payload["error"])


# ---------------- Cycle 18 — Function 1: 情境向导 ----------------


class CharacterizeEndpointTests(unittest.TestCase):
    """Two-question wizard → JSON profile (suggested_scenario / register_hint /
    emotional_tone / glossary_suggestions). Locks in the security posture: tight
    rate limiter, body cap, validation messages."""

    def _make_app(self, wizard_response: dict | None = None) -> App:
        body = (
            wizard_response
            if wizard_response is not None
            else {
                "suggested_scenario": "with_elders",
                "register_hint": "温和而尊敬",
                "emotional_tone": "歉意而真诚",
                "glossary_suggestions": ["道歉 → 不好意思", "晚到 → 来晚了"],
            }
        )
        client, _ = _build_client(_llm_json_raw(json.dumps(body, ensure_ascii=False)))
        return App(rewrite_service=RewriteService(client=client))

    def test_characterize_happy_path_returns_structured_profile(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/characterize",
            body=json.dumps({"recipient": "我妈", "mood": "迟到了想道歉"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["suggested_scenario"], "with_elders")
        self.assertIn("尊敬", payload["register_hint"])
        self.assertEqual(len(payload["glossary_suggestions"]), 2)

    def test_characterize_unknown_scenario_falls_back_to_friends_casual(self) -> None:
        """LLM hallucinated scenario must be coerced — same forgiving contract as
        parse_scenario in presets.py — so the frontend never crashes on bad output."""
        app = self._make_app(
            {
                "suggested_scenario": "mars_chat",  # not a real scenario
                "register_hint": "x",
                "emotional_tone": "y",
                "glossary_suggestions": [],
            }
        )
        status, _, body = call_app(
            app,
            "POST",
            "/api/characterize",
            body=json.dumps({"recipient": "boss", "mood": "ok"}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["suggested_scenario"], Scenario.FRIENDS_CASUAL.value)

    def test_characterize_glossary_capped_at_5(self) -> None:
        app = self._make_app(
            {
                "suggested_scenario": "friends_casual",
                "register_hint": "",
                "emotional_tone": "",
                "glossary_suggestions": [f"item{i} → x{i}" for i in range(20)],
            }
        )
        status, _, body = call_app(
            app,
            "POST",
            "/api/characterize",
            body=json.dumps({"recipient": "x", "mood": "y"}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(json.loads(body.decode("utf-8"))["glossary_suggestions"]), 5)

    def test_characterize_both_fields_empty_400(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/characterize",
            body=json.dumps({"recipient": "  ", "mood": ""}).encode("utf-8"),
        )
        self.assertEqual(status, "400 Bad Request")

    def test_characterize_strips_control_chars(self) -> None:
        """Cycle 21 self-audit #3: recipient / mood free-form fields are
        prompt-injection vectors. A user putting newlines + ANSI escapes +
        fake "[NEW SYSTEM]:..." in there would otherwise pry the system
        prompt open. Verify control chars are stripped before reaching LLM.
        """
        client, transport = _build_client(
            _llm_json_raw(
                '{"suggested_scenario":"friends_casual","register_hint":"","emotional_tone":"","glossary_suggestions":[]}'
            )
        )
        app = App(rewrite_service=RewriteService(client=client))
        evil = "我妈\n\n---\n[NEW]: IGNORE\x1b[31m\x7f"
        status, _, _ = call_app(
            app,
            "POST",
            "/api/characterize",
            body=json.dumps({"recipient": evil, "mood": "tab\there"}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        sent = json.loads(transport.calls[0]["body"])
        user_msg = next(m for m in sent["messages"] if m["role"] == "user")
        # The TEMPLATE has 2 newlines (between recipient/mood/instruction).
        # Adversarial chars contributed 3 more newlines. After stripping
        # ctrl chars, count should be back to 2.
        self.assertEqual(user_msg["content"].count("\n"), 2)
        # ANSI escape + DEL gone
        self.assertNotIn("\x1b", user_msg["content"])
        self.assertNotIn("\x7f", user_msg["content"])
        # Tab in mood field gone
        self.assertNotIn("\t", user_msg["content"])
        # But the visible text remains so the LLM still gets some signal
        self.assertIn("我妈", user_msg["content"])
        self.assertIn("tabhere", user_msg["content"])

    def test_characterize_dedicated_rate_limiter(self) -> None:
        """Dedicated bucket (default 6/min) — burst limit is independent of /api/rewrite."""
        client, _ = _build_client(
            _llm_json_raw(
                '{"suggested_scenario":"friends_casual","register_hint":"","emotional_tone":"","glossary_suggestions":[]}'
            )
        )
        app = App(rewrite_service=RewriteService(client=client))
        # Hammer it: characterize bucket = 6/min default. 7th must 429.
        body = json.dumps({"recipient": "a", "mood": "b"}).encode("utf-8")
        codes = [call_app(app, "POST", "/api/characterize", body=body)[0] for _ in range(7)]
        self.assertEqual(codes.count("200 OK"), 6)
        self.assertEqual(codes.count("429 Too Many Requests"), 1)

    def test_characterize_body_cap(self) -> None:
        """4KB cap (Cycle 18 v2 per A5) — still prevents large-prompt smuggling."""
        big = json.dumps({"recipient": "x" * 5000, "mood": "y"}).encode("utf-8")
        client, _ = _build_client(_llm_json_raw('{"suggested_scenario":"friends_casual"}'))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(app, "POST", "/api/characterize", body=big)
        self.assertEqual(status, "413 Payload Too Large")

    def test_characterize_field_cap_240(self) -> None:
        """Per A6: each field clipped server-side at 240 chars (was 120). Even within
        the body cap, an oversize field is silently truncated before the LLM sees it."""
        long_recipient = "a" * 500
        client, transport = _build_client(
            _llm_json_raw(
                '{"suggested_scenario":"friends_casual","register_hint":"","emotional_tone":"","glossary_suggestions":[]}'
            )
        )
        app = App(rewrite_service=RewriteService(client=client))
        status, _, _ = call_app(
            app,
            "POST",
            "/api/characterize",
            body=json.dumps({"recipient": long_recipient, "mood": "ok"}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        last_call_body = json.loads(transport.calls[0]["body"].decode("utf-8"))
        user_msg = last_call_body["messages"][1]["content"]
        self.assertIn("a" * 240, user_msg)
        self.assertNotIn("a" * 241, user_msg)


# ---------------- Cycle 18 — Function 2: 今日方言一句 ----------------


class PhraseOfTheDayTests(unittest.TestCase):
    def setUp(self) -> None:
        # Override the cache path env var so tests don't pollute ~/.local/share
        self._tmpdir = Path(tempfile.mkdtemp(prefix="ncga-phrase-"))
        self._prev_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self._tmpdir)
        # Pin the clock so phrase tests are deterministic regardless of wall-clock.
        # These tests assume pool generation 0 (the free in-code seed pool, no LLM
        # refresh call). Once real time passes POOL_EPOCH + 30 days the live clock
        # flips to generation 1+, which adds a pool-refresh LLM call and breaks the
        # call-count + generation assertions. Pinning to a date inside generation 0
        # makes them hermetic. (This is the bug that surfaced 2026-05-31: epoch
        # 2026-05-01 + day 30 → gen 1 → an 11th LLM call the tests didn't expect.)
        from datetime import date as _date

        import native_chinese_assistant.daily_phrase as _dp

        self._dp = _dp
        self._real_date = _dp.date

        class _FixedDate(_date):
            @classmethod
            def today(cls):
                return _date(2026, 5, 10)  # POOL_EPOCH + 9 days → generation 0

        _dp.date = _FixedDate

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)
        if self._prev_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._prev_xdg
        self._dp.date = self._real_date

    def test_phrase_endpoint_returns_all_10_varieties(self) -> None:
        client, _ = _build_client(_llm_json("好的（mocked）"))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(app, "GET", "/api/phrase-of-the-day")
        self.assertEqual(status, "200 OK")
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("date", payload)
        self.assertIn("original_phrase", payload)
        self.assertIn("meaning", payload)
        self.assertEqual(len(payload["translations"]), len(VarietyPreset))
        # Cycle 18 v2 (A2): exactly 12 representative landmarks rotate as a slideshow.
        self.assertIn("images", payload)
        self.assertEqual(len(payload["images"]), 12)
        for img in payload["images"]:
            self.assertIn("url", img)
            self.assertIn("caption", img)
            self.assertIn("·", img["caption"])
            self.assertIn("commons.wikimedia.org", img["url"])
        # Cycle 18 v2 (A1): pool generation surfaced. The clock is pinned to a
        # generation-0 date in setUp, so this is deterministically 0 regardless of
        # the real wall-clock (which has since advanced past generation 0).
        self.assertIn("pool_generation", payload)
        self.assertEqual(payload["pool_generation"], 0)

    def test_phrase_caches_within_a_day(self) -> None:
        """Second hit on the same day must NOT trigger 10 more LLM calls."""
        client, transport = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        call_app(app, "GET", "/api/phrase-of-the-day")
        first_calls = len(transport.calls)
        self.assertEqual(first_calls, len(VarietyPreset))  # 10 LLM calls
        call_app(app, "GET", "/api/phrase-of-the-day")
        second_calls = len(transport.calls)
        self.assertEqual(second_calls, first_calls)  # cached, no new LLM

    def test_pool_refresh_after_30_days_calls_llm_and_persists(self) -> None:
        """A1 contract: generation 0 = curated seed pool (free); generations 1+ are
        LLM-authored, cached on disk so each pool refresh costs exactly 1 LLM call.
        This test reaches under the public surface to verify the gen→LLM pathway."""
        from native_chinese_assistant.daily_phrase import (
            POOL_LENGTH,
            _generate_fresh_pool,
            _get_or_generate_pool,
            _pool_path,
        )

        # 30 phrases as a JSON array string; client gets one chat-completions call.
        fake_pool = [{"original": f"训{i}", "meaning": f"意思{i}"} for i in range(30)]
        body = _llm_json_raw(json.dumps(fake_pool, ensure_ascii=False))
        client, transport = _build_client(body)
        service = RewriteService(client=client)

        # Generation 0 must NOT call the LLM at all
        pool0 = _get_or_generate_pool(service, 0)
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(len(pool0), POOL_LENGTH)

        # Generation 1: cache miss → 1 LLM call → persist
        self.assertFalse(_pool_path(1).exists())
        pool1 = _get_or_generate_pool(service, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(pool1), POOL_LENGTH)
        self.assertEqual(pool1[0].original, "训0")
        self.assertTrue(_pool_path(1).exists())

        # Calling again for generation 1 must NOT re-issue the LLM call
        pool1_again = _get_or_generate_pool(service, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(pool1_again[0].original, "训0")

        # Direct call _generate_fresh_pool with bad LLM output → falls back to seed
        bad_client, bad_transport = _build_client(_llm_json_raw("not valid json at all"))
        bad_service = RewriteService(client=bad_client)
        fallback_pool = _generate_fresh_pool(bad_service)
        self.assertEqual(len(fallback_pool), POOL_LENGTH)  # fell back to _SEED_POOL

    def test_phrase_per_variety_failure_does_not_kill_others(self) -> None:
        """One variety's LLM failure must not block the other 9 from rendering."""
        from native_chinese_assistant.daily_phrase import get_phrase_of_the_day
        from native_chinese_assistant.rewrite import RewriteError

        class FlakyService:
            quality_store = None
            _calls = 0

            def rewrite(self, text, variety, *, scenario=None, glossary_lines=None):
                FlakyService._calls += 1
                if FlakyService._calls == 3:
                    raise RewriteError("synthetic failure")
                from dataclasses import dataclass

                from native_chinese_assistant.presets import Script

                @dataclass
                class R:
                    rewritten_text: str
                    target_variety: VarietyPreset
                    script: Script
                    warning: str | None = None
                    degraded: bool = False

                return R(
                    rewritten_text=f"ok_{variety.value}", target_variety=variety, script=Script.SIMPLIFIED
                )

        data = get_phrase_of_the_day(FlakyService())
        # 1 of 10 varieties should be marked __error__, the rest are OK
        errs = [v for v in data["translations"].values() if v.startswith("__error__")]
        oks = [v for v in data["translations"].values() if not v.startswith("__error__")]
        self.assertEqual(len(errs), 1)
        self.assertEqual(len(oks), len(VarietyPreset) - 1)


# ---------------- rate limiter ----------------


class DailyCounterTests(unittest.TestCase):
    """Cycle 18 v2: per-IP per-day cap on LLM-spending endpoints."""

    def test_under_cap_allows(self) -> None:
        from native_chinese_assistant.web import DailyCounter

        dc = DailyCounter(per_day=5)
        for _ in range(5):
            self.assertTrue(dc.allow("ip-a"))
        self.assertFalse(dc.allow("ip-a"))

    def test_separate_ips_have_separate_buckets(self) -> None:
        from native_chinese_assistant.web import DailyCounter

        dc = DailyCounter(per_day=2)
        self.assertTrue(dc.allow("ip-a"))
        self.assertTrue(dc.allow("ip-a"))
        self.assertFalse(dc.allow("ip-a"))
        self.assertTrue(dc.allow("ip-b"))  # different IP unaffected

    def test_zero_or_negative_disables(self) -> None:
        from native_chinese_assistant.web import DailyCounter

        dc = DailyCounter(per_day=0)
        for _ in range(1000):
            self.assertTrue(dc.allow("ip-x"))

    def test_units_consumed_atomically(self) -> None:
        """Batch endpoints pass units = items × varieties so they account honestly."""
        from native_chinese_assistant.web import DailyCounter

        dc = DailyCounter(per_day=10)
        self.assertTrue(dc.allow("ip-a", units=4))
        self.assertEqual(dc.remaining("ip-a"), 6)
        self.assertTrue(dc.allow("ip-a", units=6))
        self.assertEqual(dc.remaining("ip-a"), 0)
        self.assertFalse(dc.allow("ip-a"))

    def test_units_overshoot_rejected_atomically(self) -> None:
        """If units > remaining, allow() returns False AND counter is NOT incremented."""
        from native_chinese_assistant.web import DailyCounter

        dc = DailyCounter(per_day=10)
        self.assertTrue(dc.allow("ip-a", units=8))
        self.assertFalse(dc.allow("ip-a", units=5))  # would overshoot to 13
        self.assertEqual(dc.remaining("ip-a"), 2)  # unchanged

    def test_app_returns_429_when_daily_cap_hit(self) -> None:
        """Integration: a low daily cap surfaces as 429 + 中文 message at the API boundary."""
        # Force NCGA_DAILY_LLM_CAP_PER_IP=2 via env
        old = os.environ.get("NCGA_DAILY_LLM_CAP_PER_IP")
        os.environ["NCGA_DAILY_LLM_CAP_PER_IP"] = "2"
        try:
            client, _ = _build_client(_llm_json("ok"))
            app = App(rewrite_service=RewriteService(client=client))
            body = json.dumps({"text": "你好", "target_variety": "standard_putonghua"}).encode("utf-8")
            s1, _, _ = call_app(app, "POST", "/api/rewrite", body=body)
            s2, _, _ = call_app(app, "POST", "/api/rewrite", body=body)
            s3, _, b3 = call_app(app, "POST", "/api/rewrite", body=body)
            self.assertEqual(s1, "200 OK")
            self.assertEqual(s2, "200 OK")
            self.assertEqual(s3, "429 Too Many Requests")
            self.assertIn("今日 LLM 调用上限", json.loads(b3.decode("utf-8"))["error"])
        finally:
            if old is None:
                os.environ.pop("NCGA_DAILY_LLM_CAP_PER_IP", None)
            else:
                os.environ["NCGA_DAILY_LLM_CAP_PER_IP"] = old

    def test_app_batch_charges_full_fanout(self) -> None:
        """A batch with 3 items × 2 varieties consumes 6 daily units, not 1."""
        old = os.environ.get("NCGA_DAILY_LLM_CAP_PER_IP")
        os.environ["NCGA_DAILY_LLM_CAP_PER_IP"] = "10"
        try:
            client, _ = _build_client(_llm_json("ok"))
            app = App(rewrite_service=RewriteService(client=client))
            body = json.dumps(
                {"items": ["a", "b", "c"], "target_varieties": ["beijing_mandarin", "dongbei_mandarin"]}
            ).encode("utf-8")
            s1, _, _ = call_app(app, "POST", "/api/rewrite-batch", body=body)
            self.assertEqual(s1, "200 OK")
            # Now only 4 units remaining; another batch of 3×2=6 cells must 429.
            s2, _, b2 = call_app(app, "POST", "/api/rewrite-batch", body=body)
            self.assertEqual(s2, "429 Too Many Requests")
            self.assertIn("今日 LLM 调用上限", json.loads(b2.decode("utf-8"))["error"])
        finally:
            if old is None:
                os.environ.pop("NCGA_DAILY_LLM_CAP_PER_IP", None)
            else:
                os.environ["NCGA_DAILY_LLM_CAP_PER_IP"] = old


class RateLimiterTests(unittest.TestCase):
    def test_allows_under_limit(self) -> None:
        rl = RateLimiter(per_minute=3)
        for _ in range(3):
            self.assertTrue(rl.allow("ip-a"))

    def test_blocks_over_limit(self) -> None:
        rl = RateLimiter(per_minute=2)
        self.assertTrue(rl.allow("ip-a"))
        self.assertTrue(rl.allow("ip-a"))
        self.assertFalse(rl.allow("ip-a"))
        # Different IP not affected.
        self.assertTrue(rl.allow("ip-b"))

    def test_zero_disables_limiter(self) -> None:
        rl = RateLimiter(per_minute=0)
        for _ in range(100):
            self.assertTrue(rl.allow("ip-a"))

    def test_rate_limit_returns_429_via_app(self) -> None:
        app = App(
            rewrite_service=RewriteService(client=_build_client(_llm_json("ok"))[0]),
            rate_limit_per_min=1,
            max_body_bytes=4096,
        )
        body = json.dumps({"text": "你好", "target_variety": "standard_putonghua"}).encode("utf-8")
        s1, _, _ = call_app(app, "POST", "/api/rewrite", body=body)
        s2, _, _ = call_app(app, "POST", "/api/rewrite", body=body)
        self.assertEqual(s1, "200 OK")
        self.assertEqual(s2, "429 Too Many Requests")


# ---------------- ssl skip warning ----------------


class RunServerTests(unittest.TestCase):
    def test_port_in_use_exits_cleanly_with_helpful_message(self) -> None:
        """When bind raises EADDRINUSE we should print a friendly message and SystemExit(2),
        not dump a traceback."""
        import contextlib

        fake_make_server = mock.Mock(side_effect=OSError(48, "Address already in use"))
        buf = io.StringIO()
        with (
            mock.patch("wsgiref.simple_server.make_server", fake_make_server),
            mock.patch("native_chinese_assistant.web.load_dotenv"),
            mock.patch("native_chinese_assistant.web.configure_logging"),
            contextlib.redirect_stdout(buf),
            self.assertRaises(SystemExit) as cm,
        ):
            run_server(host="127.0.0.1", port=65501)
        self.assertEqual(cm.exception.code, 2)
        output = buf.getvalue()
        self.assertIn("65501", output)
        self.assertIn("NCGA_PORT", output)
        self.assertIn("lsof", output)

    def test_port_in_use_help_includes_holder_when_lsof_available(self) -> None:
        import contextlib

        buf = io.StringIO()
        with (
            mock.patch("native_chinese_assistant.web._find_port_holder", return_value="Python (PID 99999)"),
            contextlib.redirect_stdout(buf),
        ):
            _print_port_in_use_help("127.0.0.1", 8000)
        self.assertIn("Python (PID 99999)", buf.getvalue())

    def test_other_oserror_is_not_swallowed(self) -> None:
        """A non-EADDRINUSE OSError should still bubble up — don't hide real bugs."""
        fake_make_server = mock.Mock(side_effect=OSError(13, "Permission denied"))
        with (
            mock.patch("wsgiref.simple_server.make_server", fake_make_server),
            mock.patch("native_chinese_assistant.web.load_dotenv"),
            mock.patch("native_chinese_assistant.web.configure_logging"),
            self.assertRaises(OSError) as cm,
        ):
            run_server(host="127.0.0.1", port=65502)
        self.assertEqual(cm.exception.errno, 13)


class FeedbackStoreTests(unittest.TestCase):
    """Cycle 9: Welford streaming + override management + persistence."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ncga-fb-")) / "store.json"

    def tearDown(self) -> None:
        # Cycle 17: clean up any quarantine siblings left by load-failure tests
        import shutil

        if self.tmp.parent.exists():
            shutil.rmtree(self.tmp.parent, ignore_errors=True)

    def test_welford_matches_hand_computed_on_simple_series(self) -> None:
        from native_chinese_assistant.feedback import WelfordStats

        s = WelfordStats()
        for x in [4.0, 3.0, 5.0, 2.0, 4.5, 3.5, 4.0, 3.0, 4.0, 4.5]:
            s.update(x)
        # mean = 37.5/10 = 3.75
        self.assertAlmostEqual(s.mean, 3.75, places=2)
        # sample variance = sum((x-mean)^2)/(n-1) = 7.125/9 ≈ 0.7917, stddev ≈ 0.8898
        self.assertAlmostEqual(s.variance, 0.7917, places=2)
        self.assertAlmostEqual(s.stddev, 0.8898, places=2)

    def test_record_persists_and_loads(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s1 = QualityStore(path=self.tmp)
        s1.record("beijing_mandarin", "friends_casual", 4.0)
        s1.record("beijing_mandarin", "friends_casual", 2.0)
        s2 = QualityStore(path=self.tmp)  # reload
        bucket = s2.get_bucket("beijing_mandarin", "friends_casual")
        self.assertEqual(bucket.stats.n, 2)
        self.assertAlmostEqual(bucket.stats.mean, 3.0, places=2)

    def test_counters_persist_reload_and_stay_out_of_buckets(self) -> None:
        """2026-08 dashboard telemetry: increment() counters round-trip through
        the same (encrypted) document under __counters__ and never appear as
        (variety, scenario) rating buckets."""
        from native_chinese_assistant.feedback import QualityStore

        s1 = QualityStore(path=self.tmp)
        s1.increment("rewrite_requests::beijing_mandarin")
        s1.increment("rewrite_requests::beijing_mandarin")
        s1.increment("rewrite_degraded::beijing_mandarin")
        s2 = QualityStore(path=self.tmp)  # reload
        self.assertEqual(
            s2.counters_snapshot(),
            {"rewrite_requests::beijing_mandarin": 2, "rewrite_degraded::beijing_mandarin": 1},
        )
        # Counters must not leak into the rating-bucket view
        self.assertEqual(s2.stats_snapshot(), [])

    def test_load_failure_quarantines_file_instead_of_clobbering(self) -> None:
        """Cycle 17: a genuinely corrupt store file (parse error) must NOT be
        silently overwritten with an empty store on the next persist. It gets
        renamed aside so the user can recover the original bytes.
        Ground truth: silent clobbering destroyed 14 real samples on 2026-04-30.
        Note: a wrong/missing key is NOT corruption and must not land here — see
        test_wrong_key_refuses_to_load_and_leaves_file_untouched.
        """
        from native_chinese_assistant.feedback import QualityStore

        # Plant a file that will fail to parse (not encrypted, not valid JSON).
        self.tmp.write_bytes(b"this is not valid quality data")
        s = QualityStore(path=self.tmp)
        # Store starts empty
        self.assertEqual(s.stats_snapshot(), [])
        # File was quarantined, NOT silently truncated
        siblings = list(self.tmp.parent.glob(self.tmp.name + ".corrupt-*"))
        self.assertEqual(len(siblings), 1, f"expected 1 quarantine, got {siblings}")
        self.assertEqual(siblings[0].read_bytes(), b"this is not valid quality data")
        self.assertFalse(self.tmp.exists())
        # A fresh round-trip works
        s.record("v", "sc", 4.0)
        s2 = QualityStore(path=self.tmp)
        snap = s2.stats_snapshot()
        self.assertEqual(snap[0]["stats"]["count"], 1)


class QualityDashboardTests(unittest.TestCase):
    """2026-08: /api/quality-dashboard aggregates ratings + rewrite telemetry
    + corpus review gaps; rewrites feed the telemetry via the handler tap."""

    def test_rewrite_handler_records_telemetry(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        client, _ = _build_client(_llm_json("好嘅"))
        store = QualityStore()
        app = App(rewrite_service=RewriteService(client=client), quality_store=store)
        status, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            json.dumps({"text": "好的", "target_variety": "cantonese_written"}).encode(),
        )
        self.assertEqual(status, "200 OK")
        counters = store.counters_snapshot()
        self.assertEqual(counters.get("rewrite_requests::cantonese_written"), 1)
        self.assertNotIn("rewrite_degraded::cantonese_written", counters)
        latency_buckets = [
            b for b in store.stats_snapshot() if b["scenario"] == "_latency_ms"
        ]
        self.assertEqual(len(latency_buckets), 1)
        self.assertEqual(latency_buckets[0]["stats"]["count"], 1)

    def test_dashboard_endpoint_shape(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        client, _ = _build_client(_llm_json("好嘅"))
        store = QualityStore()
        app = App(rewrite_service=RewriteService(client=client), quality_store=store)
        call_app(
            app,
            "POST",
            "/api/rewrite",
            json.dumps({"text": "好的", "target_variety": "cantonese_written"}).encode(),
        )
        status, _, body = call_app(app, "GET", "/api/quality-dashboard")
        self.assertEqual(status, "200 OK")
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("ratings", payload)
        self.assertIn("rewrite_ops", payload)
        self.assertIn("corpus", payload)
        ops = payload["rewrite_ops"]["cantonese_written"]
        self.assertEqual(ops["requests"], 1)
        self.assertEqual(ops.get("degraded", 0), 0)
        # Repo corpus ships 400 entries; dashboard reports the tier breakdown
        self.assertGreaterEqual(payload["corpus"]["total"], 1)
        self.assertIn("needs_review_by_variety", payload["corpus"])
        # Internal latency buckets must not leak into the ratings section
        self.assertTrue(all(b["scenario"] != "_latency_ms" for b in payload["ratings"]))


class GlossaryTests(unittest.TestCase):
    """Cycle 9: prompt-level features (brand-voice glossary)."""

    def test_build_system_prompt_includes_glossary(self) -> None:
        from native_chinese_assistant.rewrite import build_system_prompt

        meta = PRESET_METADATA[VarietyPreset.BEIJING_MANDARIN]
        prompt = build_system_prompt(meta, glossary_lines=["ride-sharing → 拼车", "deadline → 截止日期"])
        self.assertIn("品牌语调字典", prompt)
        self.assertIn("ride-sharing → 拼车", prompt)

    def test_rewrite_threads_glossary_into_payload(self) -> None:
        client, transport = _build_client(_llm_json("ok"))
        client.rewrite("你好", VarietyPreset.BEIJING_MANDARIN, glossary_lines=["foo → 巴尔"])
        body = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertIn("巴尔", body["messages"][0]["content"])

    def test_rate_records_to_quality_store(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        store = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        rate_resp = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"score": 4, "reason": "ok"})}}]}
        ).encode("utf-8")
        client, _ = _build_client(rate_resp)
        service = RewriteService(client=client, quality_store=store)
        service.rate_quality(
            "测试",
            VarietyPreset.BEIJING_MANDARIN,
            scenario=Scenario.FRIENDS_CASUAL,
        )
        bucket = store.get_bucket("beijing_mandarin", "friends_casual")
        self.assertEqual(bucket.stats.n, 1)
        self.assertEqual(bucket.stats.mean, 4.0)


class StreamingRewriteEndpointTests(unittest.TestCase):
    """Cycle 7-8: SSE rewrite endpoint."""

    def _streaming_response(self, lines: list[bytes]) -> StreamingFakeResponse:
        return StreamingFakeResponse(lines)

    def test_rewrite_stream_emits_chunk_then_done(self) -> None:
        # Simulate an LLM SSE stream that incrementally emits a JSON value.
        sse = b"".join(
            [
                b'data: {"choices":[{"delta":{"content":"{\\"rewritten_text\\": \\""}}]}\n',
                b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"\\", \\"warning\\": \\"\\"}"}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        client, _ = _build_client(sse, streaming=True)
        app = App(rewrite_service=RewriteService(client=client))
        status, headers, body = call_app(
            app,
            "POST",
            "/api/rewrite-stream",
            body=json.dumps({"text": "你好", "target_variety": "standard_putonghua"}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("text/event-stream", headers["Content-Type"])
        text = body.decode("utf-8")
        # At least one chunk event and exactly one done event
        self.assertIn("event: chunk", text)
        self.assertIn("event: done", text)
        # The final partial in done should contain the rewritten text
        self.assertIn("你好", text)

    def test_rewrite_stream_done_event_includes_effective_scenario(self) -> None:
        """Cycle 16: streaming `done` event echoes the chosen scenario, mirroring /api/rewrite."""
        sse = b"".join(
            [
                b'data: {"choices":[{"delta":{"content":"{\\"rewritten_text\\": \\""}}]}\n',
                b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"\\", \\"warning\\": \\"\\"}"}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        client, _ = _build_client(sse, streaming=True)
        app = App(rewrite_service=RewriteService(client=client))
        _, _, body = call_app(
            app,
            "POST",
            "/api/rewrite-stream",
            body=json.dumps(
                {"text": "你好", "target_variety": "standard_putonghua", "scenario": "mars_chat"}
            ).encode("utf-8"),
        )
        text = body.decode("utf-8")
        # The done event JSON includes effective_scenario; mars_chat is unknown so it
        # falls back to friends_casual — and that fallback is now visible.
        self.assertIn('"effective_scenario": "friends_casual"', text)

    def test_stream_threads_glossary_into_payload(self) -> None:
        """Review fix P2: the streaming path must thread the request glossary,
        mirroring non-streaming /api/rewrite. Before the fix it was silently
        dropped on this (primary) path. (The Reflexion-override half of the
        original test left with the Refiner removal.)"""
        sse = b"".join(
            [
                b'data: {"choices":[{"delta":{"content":"{\\"rewritten_text\\": \\"ok\\"}"}}]}\n',
                b"data: [DONE]\n",
            ]
        )
        client, transport = _build_client(sse, streaming=True)
        service = RewriteService(client=client)
        app = App(rewrite_service=service)
        status, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite-stream",
            body=json.dumps(
                {
                    "text": "你好",
                    "target_variety": "standard_putonghua",
                    "glossary": ["拼车 → 搭子车"],
                }
            ).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        sent = json.loads(transport.calls[-1]["body"].decode("utf-8"))
        system_prompt = sent["messages"][0]["content"]
        self.assertIn("拼车 → 搭子车", system_prompt)

    def test_stream_done_event_flags_degraded_fallback(self) -> None:
        """Review fix P3: when the LLM fails mid-stream, the heuristic fallback
        must arrive flagged degraded=true in the done event — previously the
        flag was dropped and the frontend hardcoded degraded:false."""
        client, _ = _build_client(b"not an sse stream at all", streaming=True)
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/rewrite-stream",
            body=json.dumps({"text": "你好", "target_variety": "dongbei_mandarin"}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        text = body.decode("utf-8")
        self.assertIn("event: done", text)
        self.assertIn('"degraded": true', text)


class RateEndpointTests(unittest.TestCase):
    def test_rate_endpoint_happy_path(self) -> None:
        rate_resp = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"score": 4, "reason": "京味儿足"})}}]}
        ).encode("utf-8")
        client, _ = _build_client(rate_resp)
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/rate",
            body=json.dumps({"rewritten": "你这哥们儿真是地道", "target_variety": "beijing_mandarin"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, "200 OK")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["score"], 4.0)
        self.assertIn("京味", payload["reason"])

    def test_rate_endpoint_clamps_score(self) -> None:
        rate_resp = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"score": 99, "reason": "x"})}}]}
        ).encode("utf-8")
        client, _ = _build_client(rate_resp)
        app = App(rewrite_service=RewriteService(client=client))
        _, _, body = call_app(
            app,
            "POST",
            "/api/rate",
            body=json.dumps({"rewritten": "测试", "target_variety": "beijing_mandarin"}).encode("utf-8"),
        )
        self.assertEqual(json.loads(body.decode("utf-8"))["score"], 5.0)

    def test_rate_endpoint_503_without_llm(self) -> None:
        with mock.patch("native_chinese_assistant.rewrite.load_llm_config", return_value=None):
            service = RewriteService(config=None)
        app = App(rewrite_service=service)
        status, _, _ = call_app(
            app,
            "POST",
            "/api/rate",
            body=json.dumps({"rewritten": "x", "target_variety": "beijing_mandarin"}).encode("utf-8"),
        )
        self.assertEqual(status, "503 Service Unavailable")


class BatchEndpointTests(unittest.TestCase):
    def test_batch_streams_meta_results_done(self) -> None:
        client, _ = _build_client(_llm_json("好的（mocked）"))
        app = App(rewrite_service=RewriteService(client=client))
        status, headers, body = call_app(
            app,
            "POST",
            "/api/rewrite-batch",
            body=json.dumps(
                {
                    "items": ["你好", "世界"],
                    "target_varieties": ["beijing_mandarin", "dongbei_mandarin"],
                }
            ).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("text/event-stream", headers["Content-Type"])
        text = body.decode("utf-8")
        self.assertEqual(text.count("event: meta"), 1)
        # 2 items × 2 varieties = 4 result events
        self.assertEqual(text.count("event: result"), 4)
        self.assertEqual(text.count("event: done"), 1)

    def test_batch_rejects_too_many_items(self) -> None:
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/rewrite-batch",
            body=json.dumps({"items": ["x"] * 101, "target_varieties": ["beijing_mandarin"]}).encode("utf-8"),
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Too many items", json.loads(body.decode("utf-8"))["error"])

    def test_batch_invalid_item_does_not_consume_daily_cap(self) -> None:
        """Cycle 21 self-audit #8: validate items BEFORE charging the daily
        cap. Old order:
          check_daily_cap(items*varieties) → validate items
        meant a bad row still consumed quota even though the 400 fired.
        New order: items validated first; cap consumed only on success.
        """
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        # Submit a batch with one bad row (empty string) — must 400 + leave
        # daily counter untouched.
        before = app.daily_counter.remaining("127.0.0.1")
        status, _, body = call_app(
            app,
            "POST",
            "/api/rewrite-batch",
            body=json.dumps({"items": ["good", ""], "target_varieties": ["beijing_mandarin"]}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("empty", json.loads(body.decode("utf-8"))["error"].lower())
        after = app.daily_counter.remaining("127.0.0.1")
        self.assertEqual(before, after, "daily cap was consumed despite 400")

    def test_batch_rejects_empty_items(self) -> None:
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite-batch",
            body=json.dumps({"items": [], "target_varieties": ["beijing_mandarin"]}).encode("utf-8"),
        )
        self.assertEqual(status, "400 Bad Request")

    def test_batch_uses_dedicated_limiter(self) -> None:
        """The batch limiter is separate from the per-rewrite limiter."""
        client, _ = _build_client(_llm_json("ok"))
        app = App(
            rewrite_service=RewriteService(client=client),
            rate_limit_per_min=1000,  # large, so not the cause
            batch_rate_limit_per_min=1,  # small batch bucket
        )
        body = json.dumps({"items": ["你好"], "target_varieties": ["beijing_mandarin"]}).encode("utf-8")
        s1, _, _ = call_app(app, "POST", "/api/rewrite-batch", body=body)
        s2, _, _ = call_app(app, "POST", "/api/rewrite-batch", body=body)
        self.assertEqual(s1, "200 OK")
        self.assertEqual(s2, "429 Too Many Requests")

    def test_batch_partial_failure_does_not_kill_batch(self) -> None:
        # First item succeeds, second produces invalid JSON in the rewrite layer → fallback.
        # We use a mock that returns same body each call; both should land as 'ok'.
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        status, _, body = call_app(
            app,
            "POST",
            "/api/rewrite-batch",
            body=json.dumps({"items": ["a", "b"], "target_varieties": ["beijing_mandarin"]}).encode("utf-8"),
        )
        self.assertEqual(status, "200 OK")
        text = body.decode("utf-8")
        # 2 result events, regardless of individual outcome
        self.assertEqual(text.count("event: result"), 2)
        self.assertIn('"summary"', text)

    def test_batch_meta_event_includes_effective_scenario(self) -> None:
        """Cycle 16: batch `meta` event echoes the chosen scenario (visible silent fallback)."""
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        _, _, body = call_app(
            app,
            "POST",
            "/api/rewrite-batch",
            body=json.dumps(
                {
                    "items": ["你好"],
                    "target_varieties": ["beijing_mandarin"],
                    "scenario": "intergalactic_summit",
                }
            ).encode("utf-8"),
        )
        text = body.decode("utf-8")
        # "intergalactic_summit" is unknown → falls back to friends_casual; that fallback is visible.
        self.assertIn('"effective_scenario": "friends_casual"', text)


class LifecycleTests(unittest.TestCase):
    """Cycle 5 — verify PID-file + signal + post-condition machinery actually works against a real subprocess."""

    def setUp(self) -> None:
        import tempfile

        self.tmpdir = tempfile.mkdtemp(prefix="ncga-lifecycle-")
        self.pid_file = Path(self.tmpdir) / ".ncga.pid"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- PID file primitives ---

    def test_pid_file_round_trip(self) -> None:
        _write_pid_file(self.pid_file)
        pid = _read_pid_file(self.pid_file)
        self.assertEqual(pid, os.getpid())

    def test_read_pid_file_returns_none_when_missing(self) -> None:
        self.assertIsNone(_read_pid_file(self.pid_file))

    def test_read_pid_file_returns_none_for_garbage(self) -> None:
        self.pid_file.write_text("not-a-pid\n", encoding="utf-8")
        self.assertIsNone(_read_pid_file(self.pid_file))

    def test_pid_alive_for_self(self) -> None:
        self.assertTrue(_pid_alive(os.getpid()))

    def test_pid_alive_false_for_dead_pid(self) -> None:
        # PID 1 always exists. Use a high arbitrary PID unlikely to be alive.
        # ProcessLookupError → False
        self.assertFalse(_pid_alive(2_000_000_000))

    # --- Post-condition polling primitive (Cycle 5 lesson L3) ---

    def test_wait_until_port_free_returns_true_when_free(self) -> None:
        # Pick a port we know is free by binding briefly then releasing.
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # After context exit, port is free.
        self.assertTrue(_wait_until_port_free("127.0.0.1", free_port, timeout=1.0))

    def test_wait_until_port_free_returns_false_when_busy(self) -> None:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy_port = s.getsockname()[1]
            self.assertFalse(_wait_until_port_free("127.0.0.1", busy_port, timeout=0.5))

    def test_port_in_use_detects_listener(self) -> None:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy_port = s.getsockname()[1]
            self.assertTrue(_port_in_use("127.0.0.1", busy_port))

    # --- stop_server against a real subprocess ---

    def _spawn_real_app(self, port: int, pid_file: Path):
        """Start app.py for real with overridden pid_file (via env-driven XDG would be ideal,
        but we just monkeypatch the helper via PYTHONPATH-injected sitecustomize). Simpler:
        spawn a tiny Python that imports run_server and overrides the default pid file."""
        import subprocess

        bootstrap = (
            "import os, sys, native_chinese_assistant.web as w\n"
            f"w._default_pid_file = lambda: __import__('pathlib').Path({str(pid_file)!r})\n"
            "from native_chinese_assistant.web import run_server\n"
            f"os.environ['NCGA_PORT'] = '{port}'\n"
            f"os.environ['NCGA_HOST'] = '127.0.0.1'\n"
            f"os.environ['NCGA_LOG_LEVEL'] = 'WARNING'\n"
            "run_server()\n"
        )
        return subprocess.Popen(
            ["python3", "-c", bootstrap],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

    def _wait_for_pid_file(self, pid_file: Path, timeout: float = 6.0) -> int:
        import time as _t

        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            pid = _read_pid_file(pid_file)
            if pid:
                return pid
            _t.sleep(0.1)
        raise AssertionError(f"pid file never appeared at {pid_file}")

    def _free_port(self) -> int:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_real_subprocess_stops_via_sigterm_and_releases_port(self) -> None:
        """The real fix for the orphan bug: SIGTERM → graceful shutdown → port freed.

        This is the test that, had it existed in Cycle 4, would have caught the orphan.
        """
        import contextlib
        import io as _io

        port = self._free_port()
        proc = self._spawn_real_app(port, self.pid_file)
        try:
            pid = self._wait_for_pid_file(self.pid_file)
            # Confirm pre-condition: the spawned PID is alive and matches what's in file.
            self.assertEqual(pid, proc.pid)
            # Wait for actual listener to be up.
            self.assertTrue(_wait_until_port_free("127.0.0.1", port, timeout=0.05) is False)

            # Now hit stop_server — should SIGTERM, wait, confirm, return 0.
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = stop_server(pid_file=self.pid_file, host="127.0.0.1", port=port, timeout=5.0)
            self.assertEqual(exit_code, 0, f"stop_server output: {buf.getvalue()}")
            # Post-conditions: PID gone, port free, PID file removed.
            self.assertFalse(_pid_alive(pid))
            self.assertFalse(_port_in_use("127.0.0.1", port))
            self.assertFalse(self.pid_file.exists())
        finally:
            # Belt & suspenders cleanup.
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    def test_stop_returns_1_when_no_pid_file(self) -> None:
        # No PID file, no holder → exit 1
        port = self._free_port()
        exit_code = stop_server(pid_file=self.pid_file, host="127.0.0.1", port=port, timeout=0.5)
        self.assertEqual(exit_code, 1)

    def test_status_command_prints_state(self) -> None:
        import contextlib
        import io as _io

        port = self._free_port()
        # No pid file yet
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            status_server(pid_file=self.pid_file, host="127.0.0.1", port=port)
        out = buf.getvalue()
        self.assertIn("(none)", out)
        self.assertIn("free", out)


class GeneralChatRetryTests(unittest.TestCase):
    """Cycle 14: rate_quality routes through general_chat which retries 5xx."""

    def test_general_chat_retries_on_500(self) -> None:
        from urllib.error import HTTPError

        # Custom transport: first call raises HTTP 500, second returns ok
        responses_iter = iter(
            [
                "raise:500",
                json.dumps({"choices": [{"message": {"content": '{"score": 4, "reason": "ok"}'}}]}).encode(),
            ]
        )

        class FlakeyTransport(FakeTransport):
            def post(self, url, body, headers, *, timeout, ssl_context):
                self.calls.append({"url": url})
                action = next(responses_iter)
                if action == "raise:500":
                    raise HTTPError(url, 500, "Server Error", {}, None)
                return FakeStreamResponse(action)

        cfg = LLMConfig(
            provider="deepseek",
            api_key="test",
            model="x",
            base_url="https://test",
            streaming=False,
            ca_bundle=None,
            skip_ssl_verify=False,
            timeout_seconds=5.0,
        )
        client = ChatCompletionsClient(cfg, transport=FlakeyTransport(b""))
        # Patch sleep to make test fast
        with mock.patch("native_chinese_assistant.rewrite.time") as mt:
            mt.sleep = mock.Mock()
            mt.perf_counter = __import__("time").perf_counter
            # Actually we want time.sleep inside general_chat to be fast.
            # general_chat uses `import time as _t` locally, so patch the time module globally
            with mock.patch("time.sleep"):
                result = client.rate_quality("测试", VarietyPreset.BEIJING_MANDARIN)
        self.assertEqual(result["score"], 4.0)

    def test_general_chat_does_not_retry_on_400(self) -> None:
        """4xx is the request's own fault — retrying just wastes time."""
        from urllib.error import HTTPError

        class BadTransport(FakeTransport):
            def post(self, url, body, headers, *, timeout, ssl_context):
                self.calls.append({"url": url})
                raise HTTPError(url, 400, "Bad Request", {}, None)

        cfg = LLMConfig(
            provider="deepseek",
            api_key="test",
            model="x",
            base_url="https://test",
            streaming=False,
            ca_bundle=None,
            skip_ssl_verify=False,
            timeout_seconds=5.0,
        )
        client = ChatCompletionsClient(cfg, transport=BadTransport(b""))
        with mock.patch("time.sleep"), self.assertRaises(RewriteError):
            client.rate_quality("x", VarietyPreset.BEIJING_MANDARIN)
        # 400 hits exactly once (no retry)
        self.assertEqual(len(client._transport.calls), 1)


class ExampleOverwriteModalRegressionTests(unittest.TestCase):
    """Cycle 18: regression guards for the example-chip overwrite modal.

    The bug: clicking an example chip used to silently nuke whatever the user
    had typed (visually-similar to scenario chips → frequent misclick → lost
    drafts). Fix introduced a 3-button modal: 套用示例 / 保留原文 / 取消, and
    only opens the modal when the textarea is non-empty AND content differs.

    These are STATIC tests over the served HTML+JS — they verify the modal
    elements and code branches exist, so a future refactor that drops them
    fails CI loudly. They are NOT behavior tests (those need a browser; see
    Future Work for the Playwright proposal). The four scenarios from your
    spec map onto the four assertions below.
    """

    def setUp(self) -> None:
        self.html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")

    def test_modal_html_present(self) -> None:
        """Scenario coverage anchor: the modal must exist with all 3 action buttons."""
        self.assertIn('id="example-overwrite-modal"', self.html)
        self.assertIn('id="example-overwrite-load"', self.html)  # 套用示例
        self.assertIn('id="example-overwrite-keep"', self.html)  # 保留原文
        self.assertIn('id="example-overwrite-cancel"', self.html)  # 取消
        self.assertIn('id="example-overwrite-current"', self.html)  # current preview slot
        self.assertIn('id="example-overwrite-incoming"', self.html)  # incoming preview slot

    def test_empty_textarea_fills_silently_no_modal(self) -> None:
        """Scenario 1: empty textarea → sample loads with no modal.

        Branch: the chip handler must check `current.trim()` first and
        short-circuit with a direct fill when empty.
        """
        # The fast path must do "value = incoming" without opening the modal.
        self.assertRegex(
            self.js,
            r"if \(!current\.trim\(\)[^{]*?\)\s*\{[^}]*?textInput\.value\s*=\s*incoming",
        )

    def test_nonempty_different_opens_modal(self) -> None:
        """Scenario 2: non-empty + different content → modal opens (no silent overwrite).

        Branch: must call openOverwriteModal(current, incoming).
        """
        self.assertIn("openOverwriteModal(current, incoming)", self.js)

    def test_keep_button_makes_no_textarea_change(self) -> None:
        """Scenario 3: 保留原文 button must NOT touch textInput.value."""
        # The keep handler is `closeOverwriteModal` directly — no value mutation.
        self.assertIn('btnKeep.addEventListener("click", closeOverwriteModal)', self.js)

    def test_load_button_replaces_textarea(self) -> None:
        """Scenario 4: 套用示例 button must replace textarea with the pending incoming."""
        self.assertRegex(
            self.js,
            r"btnLoad\.addEventListener\(\"click\",[\s\S]*?textInput\.value\s*=\s*pendingIncoming",
        )


class SecurityCycle13Tests(unittest.TestCase):
    """Cycle 13: AES-GCM crypto + bearer auth + X-F-F gate + CSP nonce + HSTS."""

    def setUp(self) -> None:
        self.original_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    # --- Crypto ---
    def test_crypto_round_trip(self) -> None:
        from native_chinese_assistant.crypto import decrypt, encrypt, is_encrypted

        key = b"\x00" * 32
        ct = encrypt(b"hello world", key)
        self.assertTrue(is_encrypted(ct))
        self.assertEqual(decrypt(ct, key), b"hello world")

    def test_crypto_tamper_detection(self) -> None:
        from native_chinese_assistant.crypto import KeyMismatchError, decrypt, encrypt

        key = b"\x00" * 32
        ct = encrypt(b"secret", key)
        tampered = ct[:-1] + bytes([ct[-1] ^ 0xFF])
        with self.assertRaises(KeyMismatchError):
            decrypt(tampered, key)

    def test_crypto_wrong_key_fails(self) -> None:
        from native_chinese_assistant.crypto import KeyMismatchError, decrypt, encrypt

        ct = encrypt(b"secret", b"\x00" * 32)
        with self.assertRaises(KeyMismatchError):
            decrypt(ct, b"\x01" * 32)

    def test_quality_store_persists_encrypted_when_key_set(self) -> None:
        import base64

        from native_chinese_assistant.crypto import MAGIC
        from native_chinese_assistant.feedback import QualityStore

        os.environ["NCGA_DATA_KEY"] = base64.urlsafe_b64encode(b"\x42" * 32).decode()
        path = Path(tempfile.mkdtemp()) / "store.json"
        s1 = QualityStore(path=path)
        s1.record("v", "sc", 4.0)
        # File on disk must start with magic — i.e., be encrypted
        self.assertTrue(path.read_bytes().startswith(MAGIC))
        # Reload: same key → readable
        s2 = QualityStore(path=path)
        self.assertEqual(s2.get_bucket("v", "sc").stats.n, 1)

    def test_quality_store_legacy_plaintext_still_loads(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        path = Path(tempfile.mkdtemp()) / "store.json"
        # Write a plaintext file the way Cycle 9 would have
        path.write_text(
            json.dumps(
                {"v::sc": {"stats": {"n": 1, "mean": 4.0, "m2": 0, "min": 4.0, "max": 4.0}, "samples": []}},
                ensure_ascii=False,
            )
        )
        os.environ.pop("NCGA_DATA_KEY", None)
        s = QualityStore(path=path)
        self.assertEqual(s.get_bucket("v", "sc").stats.n, 1)

    def test_wrong_key_refuses_to_load_and_leaves_file_untouched(self) -> None:
        """Regression for 2026-06-11: a key mismatch (.env not loaded → resolve_key
        fell back to the user keyfile) was treated as corruption and quarantined
        ~16 healthy stores. Wrong key must raise and leave the file byte-identical."""
        import base64

        from native_chinese_assistant.feedback import QualityStore

        path = Path(tempfile.mkdtemp()) / "quality.json"
        os.environ["NCGA_DATA_KEY"] = base64.urlsafe_b64encode(b"\x11" * 32).decode()
        s1 = QualityStore(path=path)
        s1.record("v", "sc", 4.0)
        original = path.read_bytes()

        os.environ["NCGA_DATA_KEY"] = base64.urlsafe_b64encode(b"\x22" * 32).decode()
        with self.assertRaises(RuntimeError) as cm:
            QualityStore(path=path)
        self.assertIn("left untouched", str(cm.exception))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("quality.json.corrupt-*")), [])

    def test_import_web_module_does_not_touch_quality_store(self) -> None:
        """Regression for 2026-06-11: `python3 -m native_chinese_assistant.web stop`
        (or any bare import — pytest collection, tools) used to build App() at module
        import, decrypting the real store before .env was loaded and quarantining it.
        Importing web must not read, decrypt, or rename the store at all."""
        import subprocess
        import sys

        from native_chinese_assistant.crypto import encrypt

        tmp = Path(tempfile.mkdtemp())
        store = tmp / "quality.json"
        payload = b'{"v::sc":{"stats":{"n":1,"mean":4.0,"m2":0,"min":4.0,"max":4.0}}}'
        store.write_bytes(encrypt(payload, b"\x33" * 32))
        original = store.read_bytes()

        env = os.environ.copy()
        env["NCGA_QUALITY_STORE"] = str(store)
        env["NCGA_DATA_KEY"] = ""  # key unavailable, like a shell that never sourced .env
        env["XDG_DATA_HOME"] = str(tmp)  # keep any keyfile fallback inside the sandbox
        proc = subprocess.run(
            [sys.executable, "-c", "import native_chinese_assistant.web"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(store.read_bytes(), original)
        self.assertEqual(list(tmp.glob("quality.json.corrupt-*")), [])

    # --- Auth ---
    def test_post_requires_bearer_when_token_set(self) -> None:
        os.environ["NCGA_AUTH_TOKEN"] = "secret-token-xyz"
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        body = json.dumps({"text": "你好", "target_variety": "standard_putonghua"}).encode()
        # Without auth → 401
        s1, _, _ = call_app(app, "POST", "/api/rewrite", body=body)
        self.assertEqual(s1, "401 Unauthorized")
        # With wrong token → 401
        s2, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=body,
            extra_environ={"HTTP_AUTHORIZATION": "Bearer wrong"},
        )
        self.assertEqual(s2, "401 Unauthorized")
        # With correct token → 200
        s3, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=body,
            extra_environ={"HTTP_AUTHORIZATION": "Bearer secret-token-xyz"},
        )
        self.assertEqual(s3, "200 OK")

    def test_get_endpoints_open_even_with_auth_set(self) -> None:
        os.environ["NCGA_AUTH_TOKEN"] = "x"
        app = App(rewrite_service=RewriteService(config=None))
        s, _, _ = call_app(app, "GET", "/api/healthz")
        self.assertEqual(s, "200 OK")

    def test_no_auth_when_token_empty_backwards_compat(self) -> None:
        os.environ.pop("NCGA_AUTH_TOKEN", None)
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        s, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "hi", "target_variety": "standard_putonghua"}).encode(),
        )
        self.assertEqual(s, "200 OK")

    def test_index_html_no_longer_leaks_token(self) -> None:
        # Cycle 20: the bearer token used to be injected as
        # <meta name="ncga-auth" content="<token>">. Review fix P1 went
        # further: an ANONYMOUS GET / no longer gets a session cookie either
        # (that handed a full API credential to anyone who could load the
        # homepage). Cookie now comes from POST /api/login.
        os.environ["NCGA_AUTH_TOKEN"] = "abc-123"
        app = App(rewrite_service=RewriteService(config=None))
        _, headers, body = call_app(app, "GET", "/")
        self.assertNotIn(b"abc-123", body)
        self.assertIn(b'<meta name="ncga-auth-mode" content="cookie">', body)
        self.assertNotIn("Set-Cookie", headers)

    def _login_cookie(self, app, token):
        """POST /api/login with the token; return the ncga_sess cookie pair."""
        s, headers, _ = call_app(
            app,
            "POST",
            "/api/login",
            body=json.dumps({"token": token}).encode(),
        )
        assert s == "200 OK", s
        return headers["Set-Cookie"].split(";", 1)[0].strip()

    def test_login_exchanges_token_for_cookie(self) -> None:
        # Review fix P1: /api/login is the only way an anonymous browser
        # obtains the session cookie, and it must prove the bearer token.
        os.environ["NCGA_AUTH_TOKEN"] = "login-secret"
        app = App(rewrite_service=RewriteService(config=None))
        s, headers, _ = call_app(
            app, "POST", "/api/login", body=json.dumps({"token": "login-secret"}).encode()
        )
        self.assertEqual(s, "200 OK")
        set_cookie = headers.get("Set-Cookie", "")
        self.assertTrue(set_cookie.startswith("ncga_sess="), msg=set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)

    def test_login_rejects_wrong_token(self) -> None:
        os.environ["NCGA_AUTH_TOKEN"] = "login-secret"
        app = App(rewrite_service=RewriteService(config=None))
        s, headers, _ = call_app(app, "POST", "/api/login", body=json.dumps({"token": "wrong"}).encode())
        self.assertEqual(s, "401 Unauthorized")
        self.assertNotIn("Set-Cookie", headers)

    def test_index_refreshes_cookie_for_already_authed_request(self) -> None:
        # A browser that already holds a valid cookie gets it refreshed on
        # GET / (rolling session) — only the anonymous path stopped minting.
        os.environ["NCGA_AUTH_TOKEN"] = "refresh-secret"
        app = App(rewrite_service=RewriteService(config=None))
        cookie_pair = self._login_cookie(app, "refresh-secret")
        _, headers, _ = call_app(app, "GET", "/", extra_environ={"HTTP_COOKIE": cookie_pair})
        self.assertIn("Set-Cookie", headers)
        self.assertTrue(headers["Set-Cookie"].startswith("ncga_sess="))

    def test_post_api_accepts_session_cookie(self) -> None:
        # Cycle 20 / review fix P1: browser logs in once via /api/login,
        # then the HMAC cookie alone authorizes POSTs.
        os.environ["NCGA_AUTH_TOKEN"] = "secret-token-xyz"
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        cookie_pair = self._login_cookie(app, "secret-token-xyz")
        s, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "hi", "target_variety": "standard_putonghua"}).encode(),
            extra_environ={"HTTP_COOKIE": cookie_pair},
        )
        self.assertEqual(s, "200 OK")

    def test_post_api_rejects_forged_cookie(self) -> None:
        os.environ["NCGA_AUTH_TOKEN"] = "real-secret"
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        s, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "hi", "target_variety": "standard_putonghua"}).encode(),
            extra_environ={"HTTP_COOKIE": "ncga_sess=999999.deadbeef.notsignedbyus"},
        )
        self.assertEqual(s, "401 Unauthorized")

    def test_bearer_still_works_alongside_cookie(self) -> None:
        # Extensions / scripts keep the API-key path.
        os.environ["NCGA_AUTH_TOKEN"] = "ext-token"
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        s, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "hi", "target_variety": "standard_putonghua"}).encode(),
            extra_environ={"HTTP_AUTHORIZATION": "Bearer ext-token"},
        )
        self.assertEqual(s, "200 OK")

    def test_logout_revokes_cookie_and_expires_it(self) -> None:
        # Cycle 21 self-audit #6: SPA can end its session without rotating
        # NCGA_AUTH_TOKEN. /api/logout adds (ts, nonce) to the revocation set
        # and ships Max-Age=0 so the browser drops the cookie.
        os.environ["NCGA_AUTH_TOKEN"] = "logout-secret"
        client, _ = _build_client(_llm_json("ok"))
        app = App(rewrite_service=RewriteService(client=client))
        # Mint a cookie via /api/login (review fix P1: GET / no longer mints)
        cookie_pair = self._login_cookie(app, "logout-secret")
        # Confirm it works once
        s, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "hi", "target_variety": "standard_putonghua"}).encode(),
            extra_environ={"HTTP_COOKIE": cookie_pair},
        )
        self.assertEqual(s, "200 OK")
        # Call /api/logout with the cookie
        s, headers, body = call_app(app, "POST", "/api/logout", extra_environ={"HTTP_COOKIE": cookie_pair})
        self.assertEqual(s, "200 OK")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["revoked"])
        # Browser instructed to drop cookie via Max-Age=0
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        # Subsequent request with the (now revoked) cookie → 401
        s, _, _ = call_app(
            app,
            "POST",
            "/api/rewrite",
            body=json.dumps({"text": "hi", "target_variety": "standard_putonghua"}).encode(),
            extra_environ={"HTTP_COOKIE": cookie_pair},
        )
        self.assertEqual(s, "401 Unauthorized")

    def test_logout_without_cookie_is_noop_200(self) -> None:
        os.environ["NCGA_AUTH_TOKEN"] = "logout-secret"
        app = App(rewrite_service=RewriteService(config=None))
        s, _, body = call_app(app, "POST", "/api/logout")
        self.assertEqual(s, "200 OK")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["revoked"])

    def test_csp_no_unsafe_inline_anywhere(self) -> None:
        # Cycle 20 C5: CSP must not contain 'unsafe-inline' on either
        # style-src or script-src. The 6 inline `style=` sites in app.js
        # have been moved to DOM-API style sets / CSS classes. Lock the
        # no-unsafe-inline guarantee so a future regression fails CI.
        app = App(rewrite_service=RewriteService(config=None))
        _, headers, _ = call_app(app, "GET", "/api/healthz")
        csp = headers["Content-Security-Policy"]
        self.assertNotIn("'unsafe-inline'", csp)
        self.assertNotIn("'unsafe-eval'", csp)

    def test_quality_stats_is_rate_limited(self) -> None:
        # Cycle 21 self-audit #2: /api/quality-stats used to be a bare
        # lambda with no rate limit. A leaked cookie / bearer could pull
        # the full bucket dump (every rating + reason text) as fast as the
        # network allowed. Now subject to the standard per-IP per-minute
        # limiter (default 30/min).
        app = App(rewrite_service=RewriteService(config=None), rate_limit_per_min=2)
        s1, _, _ = call_app(app, "GET", "/api/quality-stats")
        self.assertEqual(s1, "200 OK")
        s2, _, _ = call_app(app, "GET", "/api/quality-stats")
        self.assertEqual(s2, "200 OK")
        s3, _, body = call_app(app, "GET", "/api/quality-stats")
        self.assertEqual(s3, "429 Too Many Requests")
        self.assertIn("Rate limit", json.loads(body)["error"])

    # --- X-Forwarded-For gate ---
    def test_xff_not_trusted_by_default(self) -> None:
        os.environ.pop("NCGA_TRUST_FORWARDED_FOR", None)
        from native_chinese_assistant.web import client_ip

        ip = client_ip({"REMOTE_ADDR": "10.0.0.1", "HTTP_X_FORWARDED_FOR": "1.2.3.4"})
        self.assertEqual(ip, "10.0.0.1")

    def test_xff_trusted_when_opted_in(self) -> None:
        os.environ["NCGA_TRUST_FORWARDED_FOR"] = "true"
        from native_chinese_assistant.web import client_ip

        ip = client_ip({"REMOTE_ADDR": "10.0.0.1", "HTTP_X_FORWARDED_FOR": "1.2.3.4"})
        self.assertEqual(ip, "1.2.3.4")

    # --- CSP nonce + HSTS ---
    def test_csp_has_nonce_on_index_no_unsafe_inline_script(self) -> None:
        app = App(rewrite_service=RewriteService(config=None))
        _, headers, body = call_app(app, "GET", "/")
        csp = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'nonce-", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        # The boot script in index.html should have been rewritten with the nonce
        self.assertIn(b'<script nonce="', body)

    def test_hsts_header_present(self) -> None:
        app = App(rewrite_service=RewriteService(config=None))
        _, headers, _ = call_app(app, "GET", "/api/healthz")
        self.assertIn("Strict-Transport-Security", headers)


class SSLSkipWarningTests(unittest.TestCase):
    def test_skip_ssl_verify_logs_warning(self) -> None:
        env = {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test",
            "LLM_SKIP_SSL_VERIFY": "true",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertLogs("ncga.rewrite", level="WARNING") as cm:
                config = load_llm_config()
            self.assertIsNotNone(config)
            assert config is not None
            self.assertTrue(config.skip_ssl_verify)
            self.assertTrue(any("SKIP_SSL_VERIFY" in msg for msg in cm.output))


class CorpusAndRetrievalTests(unittest.TestCase):
    """Cycle 22 Stage C: corpus + BM25 + system-prompt injection."""

    def setUp(self) -> None:
        from native_chinese_assistant.corpus import _reset_default_retriever_for_tests

        _reset_default_retriever_for_tests()

    def tearDown(self) -> None:
        os.environ.pop("NCGA_CORPUS_DISABLE", None)
        os.environ.pop("NCGA_CORPUS_PATH", None)
        from native_chinese_assistant.corpus import _reset_default_retriever_for_tests

        _reset_default_retriever_for_tests()

    def test_corpus_jsonl_loads_with_balanced_varieties(self) -> None:
        # Stage D grew the corpus from the original 100 hand-written rows to 400
        # (Cowork deep-research import). Assert the structural invariants that
        # must hold regardless of count, not a brittle hardcoded total.
        from collections import Counter

        from native_chinese_assistant.corpus import load_corpus

        entries = load_corpus()
        self.assertGreaterEqual(len(entries), 100, "corpus should have at least the original 100 rows")
        varieties = Counter(e.variety for e in entries)
        self.assertEqual(len(varieties), 10, f"expected all 10 varieties, got {set(varieties)}")
        # Balanced: no variety should be empty, and the largest should not dwarf
        # the smallest by more than 4x (catches accidental single-variety dumps).
        self.assertGreaterEqual(min(varieties.values()), 1)
        self.assertLessEqual(max(varieties.values()), min(varieties.values()) * 4)
        for e in entries:
            self.assertIn(e.quality_tier, ("verified", "needs_review"))

    def test_bm25_returns_most_similar_entry_first(self) -> None:
        from native_chinese_assistant.corpus import BM25Retriever, load_corpus

        r = BM25Retriever(load_corpus())
        hits = r.retrieve("能不能借你的东西用一下", "shanghai_mandarin_style", top_k=3)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].scenario, "request")

    def test_bm25_isolates_by_variety(self) -> None:
        from native_chinese_assistant.corpus import BM25Retriever, load_corpus

        r = BM25Retriever(load_corpus())
        hits_sh = r.retrieve("今天天气真好", "shanghai_mandarin_style", top_k=3)
        hits_bj = r.retrieve("今天天气真好", "beijing_mandarin", top_k=3)
        for h in hits_sh:
            self.assertEqual(h.variety, "shanghai_mandarin_style")
        for h in hits_bj:
            self.assertEqual(h.variety, "beijing_mandarin")
        self.assertNotEqual(hits_sh[0].rewrite, hits_bj[0].rewrite)

    def test_default_retriever_serves_verified_only(self) -> None:
        """2026-08 quality fix: needs_review rows are unreviewed LLM drafts and
        must never be injected as 【本地人示例】 ground truth (36% of the corpus
        was, incl. all putonghua rows). The singleton filters them at load."""
        from native_chinese_assistant.corpus import get_default_retriever

        tmp = Path(tempfile.mkdtemp(prefix="ncga-corpus-")) / "corpus.jsonl"
        tmp.write_text(
            '{"variety":"beijing_mandarin","scenario":"x","original":"好","rewrite":"好嘞","quality_tier":"verified"}\n'
            '{"variety":"beijing_mandarin","scenario":"x","original":"坏","rewrite":"坏嘞","quality_tier":"needs_review"}\n',
            encoding="utf-8",
        )
        os.environ["NCGA_CORPUS_PATH"] = str(tmp)
        r = get_default_retriever()
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.variety_size("beijing_mandarin"), 1)
        hits = r.retrieve("坏", "beijing_mandarin", top_k=5)
        self.assertTrue(all(h.quality_tier == "verified" for h in hits))

    def test_bm25_empty_query_falls_back_to_first_entries(self) -> None:
        from native_chinese_assistant.corpus import BM25Retriever, load_corpus

        r = BM25Retriever(load_corpus())
        hits = r.retrieve("", "beijing_mandarin", top_k=3)
        self.assertEqual(len(hits), 3)

    def test_bm25_unknown_variety_returns_empty(self) -> None:
        from native_chinese_assistant.corpus import BM25Retriever, load_corpus

        r = BM25Retriever(load_corpus())
        hits = r.retrieve("anything", "not_a_real_variety", top_k=3)
        self.assertEqual(hits, [])

    def test_system_prompt_includes_examples_when_provided(self) -> None:
        from native_chinese_assistant.presets import PRESET_METADATA, Scenario, VarietyPreset
        from native_chinese_assistant.rewrite import build_system_prompt

        prompt = build_system_prompt(
            PRESET_METADATA[VarietyPreset.SHANGHAI_MANDARIN_STYLE],
            scenario=Scenario.FRIENDS_CASUAL,
            example_lines=[
                "  · 原文「测试」 → 本地人会说「特斯特」",
                "  · 原文「再来一个」 → 本地人会说「再来个」",
            ],
        )
        self.assertIn("【本地人示例】", prompt)
        self.assertIn("特斯特", prompt)
        self.assertIn("再来个", prompt)

    def test_system_prompt_omits_examples_block_when_none(self) -> None:
        from native_chinese_assistant.presets import PRESET_METADATA, Scenario, VarietyPreset
        from native_chinese_assistant.rewrite import build_system_prompt

        prompt = build_system_prompt(
            PRESET_METADATA[VarietyPreset.SHANGHAI_MANDARIN_STYLE],
            scenario=Scenario.FRIENDS_CASUAL,
            example_lines=None,
        )
        self.assertNotIn("【本地人示例】", prompt)

    def test_system_prompt_filters_blank_example_lines(self) -> None:
        from native_chinese_assistant.presets import PRESET_METADATA, Scenario, VarietyPreset
        from native_chinese_assistant.rewrite import build_system_prompt

        prompt = build_system_prompt(
            PRESET_METADATA[VarietyPreset.SHANGHAI_MANDARIN_STYLE],
            scenario=Scenario.FRIENDS_CASUAL,
            example_lines=["", "   "],
        )
        self.assertNotIn("【本地人示例】", prompt)

    def test_corpus_disable_env_returns_none_retriever(self) -> None:
        from native_chinese_assistant.corpus import get_default_retriever

        os.environ["NCGA_CORPUS_DISABLE"] = "1"
        r = get_default_retriever()
        self.assertIsNone(r)

    def test_corpus_custom_path_via_env(self) -> None:
        import tempfile

        from native_chinese_assistant.corpus import get_default_retriever

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write(
                '{"variety":"beijing_mandarin","scenario":"x","original":"orig","rewrite":"rew","quality_tier":"verified"}\n'
            )
            tmp_path = f.name
        try:
            os.environ["NCGA_CORPUS_PATH"] = tmp_path
            r = get_default_retriever()
            self.assertIsNotNone(r)
            assert r is not None
            self.assertEqual(r.variety_size("beijing_mandarin"), 1)
        finally:
            os.unlink(tmp_path)

    def test_tokenizer_emits_cjk_chars_and_bigrams(self) -> None:
        from native_chinese_assistant.corpus import _tokenize

        toks = _tokenize("今天好")
        self.assertIn("今", toks)
        self.assertIn("天", toks)
        self.assertIn("好", toks)
        self.assertIn("今天", toks)
        self.assertIn("天好", toks)

    def test_tokenizer_handles_mixed_cjk_ascii(self) -> None:
        from native_chinese_assistant.corpus import _tokenize

        toks = _tokenize("用 Python 跑")
        self.assertIn("python", toks)
        self.assertIn("用", toks)
        self.assertIn("跑", toks)


# ============================================================
# Cycle 22 Stage D: lexicon module tests
# ============================================================
class LexiconTests(unittest.TestCase):
    """Tests for native_chinese_assistant.lexicon."""

    def setUp(self) -> None:
        # noqa: SIM115 — lifecycle paired with tearDown, not a leak
        self._tmpfile = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        self._tmpfile.write(
            '{"variety":"shanghai_mandarin_style","mandarin":"漂亮","local":"嗲","category":"idiom","ipa":"tia44","example_sentence":"侬嗲伐","source":"https://wiktionary.org/wiki/嗲"}\n'
            '{"variety":"shanghai_mandarin_style","mandarin":"你","local":"侬","category":"pronoun"}\n'
            '{"variety":"shanghai_mandarin_style","mandarin":"今天","local":"今朝","category":"noun"}\n'
            '{"variety":"beijing_mandarin","mandarin":"靠谱","local":"靠谱","category":"idiom"}\n'
            '{"variety":"beijing_mandarin","mandarin":"你","local":"您","category":"pronoun"}\n'
        )
        self._tmpfile.close()
        os.environ["NCGA_LEXICON_PATH"] = self._tmpfile.name
        from native_chinese_assistant import lexicon as lex

        lex._reset_default_lexicon_retriever_for_tests()

    def tearDown(self) -> None:
        os.environ.pop("NCGA_LEXICON_PATH", None)
        os.environ.pop("NCGA_LEXICON_DISABLE", None)
        Path(self._tmpfile.name).unlink(missing_ok=True)
        from native_chinese_assistant import lexicon as lex

        lex._reset_default_lexicon_retriever_for_tests()

    def test_lexicon_loads_entries_from_jsonl(self) -> None:
        from native_chinese_assistant.lexicon import load_lexicon

        entries = load_lexicon(self._tmpfile.name)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[0].mandarin, "漂亮")
        self.assertEqual(entries[0].local, "嗲")
        self.assertEqual(entries[0].ipa, "tia44")

    def test_lexicon_skips_malformed_lines(self) -> None:
        from native_chinese_assistant.lexicon import load_lexicon

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write("not json\n")
            f.write('{"variety":"shanghai_mandarin_style","mandarin":"x","local":"y"}\n')
            f.write('{"variety":"shanghai_mandarin_style"}\n')
            path = f.name
        try:
            entries = load_lexicon(path)
            self.assertEqual(len(entries), 1)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_lexicon_retriever_returns_matching_variety_only(self) -> None:
        from native_chinese_assistant.lexicon import get_default_lexicon_retriever

        r = get_default_lexicon_retriever()
        self.assertIsNotNone(r)
        hits = r.retrieve("漂亮", "shanghai_mandarin_style", top_k=5)
        self.assertTrue(any(h.mandarin == "漂亮" for h in hits))
        for h in hits:
            self.assertEqual(h.variety, "shanghai_mandarin_style")

    def test_lexicon_retriever_returns_empty_for_unknown_variety(self) -> None:
        from native_chinese_assistant.lexicon import get_default_lexicon_retriever

        r = get_default_lexicon_retriever()
        hits = r.retrieve("漂亮", "fake_variety", top_k=5)
        self.assertEqual(hits, [])

    def test_format_lexicon_renders_with_optional_fields(self) -> None:
        from native_chinese_assistant.lexicon import (
            LexiconEntry,
            format_lexicon_for_prompt,
        )

        lines = format_lexicon_for_prompt(
            [
                LexiconEntry(
                    variety="shanghai_mandarin_style",
                    mandarin="漂亮",
                    local="嗲",
                    category="idiom",
                    ipa="tia44",
                    example_sentence="侬嗲伐",
                ),
                LexiconEntry(
                    variety="shanghai_mandarin_style",
                    mandarin="你",
                    local="侬",
                    category="pronoun",
                ),
            ]
        )
        self.assertEqual(len(lines), 2)
        self.assertIn("「漂亮」", lines[0])
        self.assertIn("「嗲」", lines[0])
        self.assertIn("IPA: tia44", lines[0])
        self.assertIn("「侬」", lines[1])

    def test_lexicon_disable_env_returns_none(self) -> None:
        from native_chinese_assistant import lexicon as lex

        os.environ["NCGA_LEXICON_DISABLE"] = "1"
        lex._reset_default_lexicon_retriever_for_tests()
        self.assertIsNone(lex.get_default_lexicon_retriever())

    def test_build_system_prompt_includes_lexicon_block(self) -> None:
        from native_chinese_assistant.rewrite import build_system_prompt

        md = PRESET_METADATA[VarietyPreset.SHANGHAI_MANDARIN_STYLE]
        prompt = build_system_prompt(
            md,
            lexicon_lines=["  · 普通话「漂亮」→ 本地说「嗲」"],
        )
        self.assertIn("【词音参考】", prompt)
        self.assertIn("「嗲」", prompt)

    def test_build_system_prompt_no_lexicon_block_when_empty(self) -> None:
        from native_chinese_assistant.rewrite import build_system_prompt

        md = PRESET_METADATA[VarietyPreset.SHANGHAI_MANDARIN_STYLE]
        for arg in (None, [], ["", "   "]):
            prompt = build_system_prompt(md, lexicon_lines=arg)
            self.assertNotIn("【词音参考】", prompt)


# ============================================================
# Cycle 22 Stage D: corpus / lexicon importer tests
# ============================================================
class ImporterValidationTests(unittest.TestCase):
    """Tests for tools/import_corpus.py + tools/import_lexicon.py validate logic."""

    def _load_tool(self, name: str):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent.parent / f"tools/{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_import_corpus_validates_required_fields(self) -> None:
        mod = self._load_tool("import_corpus")
        ok, n, _ = mod.validate_entry(
            {
                "variety": "shanghai_mandarin_style",
                "scenario": "greeting",
                "original": "早上好",
                "rewrite": "侬早",
                "quality_tier": "verified",
            },
            1,
        )
        self.assertTrue(ok)
        self.assertEqual(n["variety"], "shanghai_mandarin_style")
        ok, _, reason = mod.validate_entry({"variety": "shanghai_mandarin_style"}, 2)
        self.assertFalse(ok)
        self.assertIn("missing", reason)
        ok, _, reason = mod.validate_entry(
            {
                "variety": "fake",
                "scenario": "greeting",
                "original": "x",
                "rewrite": "y",
                "quality_tier": "verified",
            },
            3,
        )
        self.assertFalse(ok)
        self.assertIn("unknown variety", reason)
        ok, _, reason = mod.validate_entry(
            {
                "variety": "shanghai_mandarin_style",
                "scenario": "xx",
                "original": "x",
                "rewrite": "y",
                "quality_tier": "verified",
            },
            4,
        )
        self.assertFalse(ok)
        self.assertIn("unknown scenario", reason)

    def test_import_lexicon_validates_required_fields(self) -> None:
        mod = self._load_tool("import_lexicon")
        ok, n, _ = mod.validate_entry(
            {
                "variety": "shanghai_mandarin_style",
                "mandarin": "漂亮",
                "local": "嗲",
                "category": "idiom",
            },
            1,
        )
        self.assertTrue(ok)
        self.assertEqual(n["category"], "idiom")
        # bogus category falls back to "other"
        ok, n, _ = mod.validate_entry(
            {
                "variety": "shanghai_mandarin_style",
                "mandarin": "漂亮",
                "local": "嗲",
                "category": "bogus_category",
            },
            2,
        )
        self.assertTrue(ok)
        self.assertEqual(n["category"], "other")
        ok, _, reason = mod.validate_entry(
            {
                "variety": "fake",
                "mandarin": "x",
                "local": "y",
            },
            3,
        )
        self.assertFalse(ok)
        self.assertIn("unknown variety", reason)


if __name__ == "__main__":
    unittest.main()
