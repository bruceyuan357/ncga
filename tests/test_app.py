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

    def test_meta_refine_invalid_variety_echoes_value(self) -> None:
        status, _, body = call_app(
            self._make_app(),
            "POST",
            "/api/meta-refine",
            body=json.dumps({"target_variety": "not_a_dialect"}).encode("utf-8"),
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Unsupported target variety", payload["error"])
        self.assertIn("not_a_dialect", payload["error"])


# ---------------- rate limiter ----------------


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
        if self.tmp.exists():
            self.tmp.unlink()
        if self.tmp.parent.exists():
            self.tmp.parent.rmdir()

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
        s1.record("beijing_mandarin", "friends_casual", 4.0, "原", "改", "好")
        s1.record("beijing_mandarin", "friends_casual", 2.0, "原2", "改2", "差")
        s2 = QualityStore(path=self.tmp)  # reload
        bucket = s2.get_bucket("beijing_mandarin", "friends_casual")
        self.assertEqual(bucket.stats.n, 2)
        self.assertAlmostEqual(bucket.stats.mean, 3.0, places=2)
        self.assertEqual(len(bucket.samples), 2)

    def test_needs_reflection_below_threshold(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=self.tmp, trigger_min_count=3, trigger_threshold=3.5, reflect_cooldown_s=0)
        for x in [2.0, 2.5, 3.0]:
            s.record("v", "sc", x, "o", "r", "")
        self.assertTrue(s.needs_reflection("v", "sc"))

    def test_needs_reflection_false_when_mean_high(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=self.tmp, trigger_min_count=3, trigger_threshold=3.5, reflect_cooldown_s=0)
        for x in [4.0, 4.5, 5.0]:
            s.record("v", "sc", x, "o", "r", "")
        self.assertFalse(s.needs_reflection("v", "sc"))

    def test_set_override_blocks_reflection_until_cooldown(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=self.tmp, trigger_min_count=2, trigger_threshold=3.5, reflect_cooldown_s=300)
        s.record("v", "sc", 1.0, "o", "r", "")
        s.record("v", "sc", 2.0, "o", "r", "")
        s.set_override("v", "sc", "新指引", "test override", baseline_mean=1.5)
        self.assertFalse(s.needs_reflection("v", "sc"))
        self.assertEqual(s.get_override_addendum("v", "sc"), "新指引")

    def test_clear_override(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=self.tmp)
        s.set_override("v", "sc", "X", "r")
        self.assertTrue(s.clear_override("v", "sc"))
        self.assertIsNone(s.get_override_addendum("v", "sc"))
        self.assertFalse(s.clear_override("v", "sc"))  # already cleared

    def test_hi_lo_examples(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=self.tmp)
        for i, score in enumerate([1.0, 2.0, 3.0, 4.0, 5.0]):
            s.record("v", "sc", score, f"o{i}", f"r{i}", "")
        hi, lo = s.hi_lo_examples("v", "sc", n_each=2)
        self.assertEqual([h.score for h in hi], [5.0, 4.0])
        self.assertEqual([lr.score for lr in lo], [1.0, 2.0])


class GlossaryAndOverrideTests(unittest.TestCase):
    """Cycle 9: prompt-level features."""

    def test_build_system_prompt_uses_addendum_override(self) -> None:
        from native_chinese_assistant.rewrite import build_system_prompt

        meta = PRESET_METADATA[VarietyPreset.BEIJING_MANDARIN]
        custom = "【场景】特别测试用的指引——必须出现这个标记"
        prompt = build_system_prompt(meta, addendum_override=custom)
        self.assertIn("特别测试用的指引", prompt)

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
            record_for=("beijing_mandarin", "friends_casual"),
            original="原",
        )
        bucket = store.get_bucket("beijing_mandarin", "friends_casual")
        self.assertEqual(bucket.stats.n, 1)
        self.assertEqual(bucket.stats.mean, 4.0)


class MetaRefineTests(unittest.TestCase):
    def test_meta_refine_happy_path_sets_override(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        store = QualityStore(
            path=Path(tempfile.mkdtemp()) / "s.json",
            trigger_min_count=3,
            trigger_threshold=4.5,
        )
        for x in [2.0, 2.5, 3.0]:
            store.record("beijing_mandarin", "friends_casual", x, "原", "改", "差")

        refine_resp = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "new_addendum": "【场景】更具体的新指引：必须用儿化音 ≥3 个，禁止 emoji。",
                                    "diff_summary": "强制儿化音并禁 emoji",
                                }
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        client, _ = _build_client(refine_resp)
        service = RewriteService(client=client, quality_store=store)
        result = service.meta_refine(VarietyPreset.BEIJING_MANDARIN, Scenario.FRIENDS_CASUAL)
        self.assertIn("儿化音", result["new_addendum"])
        self.assertEqual(result["sample_count"], 3)
        # Cycle 10 change: meta_refine writes to DRAFT, not active override.
        # Active override should NOT be set yet (requires activate_override).
        self.assertIsNone(store.get_override_addendum("beijing_mandarin", "friends_casual"))
        self.assertEqual(result["status"], "draft")
        bucket = store.get_bucket("beijing_mandarin", "friends_casual")
        self.assertEqual(bucket.draft_addendum, result["new_addendum"])
        # Activating it promotes draft → override
        self.assertTrue(store.activate_override("beijing_mandarin", "friends_casual"))
        self.assertEqual(
            store.get_override_addendum("beijing_mandarin", "friends_casual"),
            result["new_addendum"],
        )
        # Draft was cleared after activation
        self.assertIsNone(store.get_bucket("beijing_mandarin", "friends_casual").draft_addendum)

    def test_set_draft_does_not_activate(self) -> None:
        """Cycle 10: meta-refine writes draft, not active override."""
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        s.set_draft("v", "sc", "新指引", "测试理由", baseline_mean=2.0)
        # Override should still be None
        self.assertIsNone(s.get_override_addendum("v", "sc"))
        # Draft is set
        bucket = s.get_bucket("v", "sc")
        self.assertEqual(bucket.draft_addendum, "新指引")
        self.assertEqual(bucket.draft_baseline_mean, 2.0)

    def test_activate_override_promotes_draft(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        # Add some samples so we have a baseline mean for A/B snapshot
        for x in [3.0, 3.5, 2.5]:
            s.record("v", "sc", x, "o", "r", "")
        s.set_draft("v", "sc", "草稿", "原因", baseline_mean=3.0)
        ok = s.activate_override("v", "sc")
        self.assertTrue(ok)
        # Override is now active
        self.assertEqual(s.get_override_addendum("v", "sc"), "草稿")
        # Draft cleared
        self.assertIsNone(s.get_bucket("v", "sc").draft_addendum)
        # Baseline was snapshotted
        bucket = s.get_bucket("v", "sc")
        self.assertEqual(bucket.activation_baseline_count, 3)
        self.assertAlmostEqual(bucket.activation_baseline_mean, 3.0, places=2)

    def test_activate_override_with_edited_addendum(self) -> None:
        """User edits the LLM's draft before activating."""
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        s.set_draft("v", "sc", "原始草稿", "r")
        ok = s.activate_override("v", "sc", addendum="人工修改后的版本", reason="手动调整")
        self.assertTrue(ok)
        self.assertEqual(s.get_override_addendum("v", "sc"), "人工修改后的版本")
        self.assertEqual(s.get_bucket("v", "sc").override_reason, "手动调整")

    def test_reject_draft_keeps_stats_clears_draft(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        s.record("v", "sc", 2.0, "o", "r", "")
        s.set_draft("v", "sc", "bad draft", "r")
        ok = s.reject_draft("v", "sc")
        self.assertTrue(ok)
        bucket = s.get_bucket("v", "sc")
        self.assertIsNone(bucket.draft_addendum)
        # Stats survived rejection
        self.assertEqual(bucket.stats.n, 1)
        # Cooldown still applied (so we don't immediately re-refine)
        self.assertGreater(bucket.last_reflected_at, 0)

    def test_ab_delta_after_activation(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        s = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        # Baseline: 3 samples averaging 2.0
        for x in [2.0, 2.0, 2.0]:
            s.record("v", "sc", x, "o", "r", "")
        s.set_draft("v", "sc", "新指引", "r")
        s.activate_override("v", "sc")
        # New samples after activation, averaging 4.0 — improvement!
        for x in [4.0, 4.0, 4.0]:
            s.record("v", "sc", x, "o", "r", "")
        ab = s.ab_delta("v", "sc")
        self.assertEqual(ab["baseline_count"], 3)
        self.assertEqual(ab["post_count"], 3)
        self.assertAlmostEqual(ab["baseline_mean"], 2.0, places=2)
        self.assertAlmostEqual(ab["post_mean"], 4.0, places=2)
        self.assertAlmostEqual(ab["delta"], 2.0, places=2)

    def test_persistence_round_trip_with_new_fields(self) -> None:
        """Cycle 10 schema additions must persist + reload."""
        from native_chinese_assistant.feedback import QualityStore

        path = Path(tempfile.mkdtemp()) / "s.json"
        s1 = QualityStore(path=path)
        for x in [2.0, 2.5]:
            s1.record("v", "sc", x, "o", "r", "")
        s1.set_draft("v", "sc", "draft", "reason", baseline_mean=2.25)
        s1.activate_override("v", "sc")
        # Add post-activation sample
        s1.record("v", "sc", 4.0, "o", "r", "")
        # Reload
        s2 = QualityStore(path=path)
        bucket = s2.get_bucket("v", "sc")
        self.assertEqual(bucket.override_addendum, "draft")
        self.assertEqual(bucket.activation_baseline_count, 2)
        self.assertAlmostEqual(bucket.activation_baseline_mean, 2.25, places=2)
        ab = s2.ab_delta("v", "sc")
        self.assertEqual(ab["post_count"], 1)
        self.assertAlmostEqual(ab["post_mean"], 4.0, places=2)

    def test_override_activate_endpoint(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        store = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        store.set_draft("beijing_mandarin", "friends_casual", "草稿规则", "test", 2.0)
        client, _ = _build_client(_llm_json("ok"))
        service = RewriteService(client=client, quality_store=store)
        app = App(rewrite_service=service, quality_store=store)
        status, _, body = call_app(
            app,
            "POST",
            "/api/override-activate",
            body=json.dumps({"target_variety": "beijing_mandarin", "scenario": "friends_casual"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(json.loads(body)["activated"])
        self.assertEqual(store.get_override_addendum("beijing_mandarin", "friends_casual"), "草稿规则")

    def test_override_reject_endpoint(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        store = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json")
        store.set_draft("beijing_mandarin", "friends_casual", "bad", "test")
        client, _ = _build_client(_llm_json("ok"))
        service = RewriteService(client=client, quality_store=store)
        app = App(rewrite_service=service, quality_store=store)
        status, _, body = call_app(
            app,
            "POST",
            "/api/override-reject",
            body=json.dumps({"target_variety": "beijing_mandarin", "scenario": "friends_casual"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(json.loads(body)["rejected"])
        self.assertIsNone(store.get_bucket("beijing_mandarin", "friends_casual").draft_addendum)

    def test_meta_refine_endpoint_400_when_too_few_samples(self) -> None:
        from native_chinese_assistant.feedback import QualityStore

        store = QualityStore(path=Path(tempfile.mkdtemp()) / "s.json", trigger_min_count=8)
        client, _ = _build_client(_llm_json("ok"))
        service = RewriteService(client=client, quality_store=store)
        app = App(rewrite_service=service, quality_store=store)
        status, _, body = call_app(
            app,
            "POST",
            "/api/meta-refine",
            body=json.dumps({"target_variety": "beijing_mandarin", "scenario": "friends_casual"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("样本不足", json.loads(body)["error"])


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
    """Cycle 14: rate_quality / meta_refine route through general_chat which retries 5xx."""

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
        from cryptography.exceptions import InvalidTag

        from native_chinese_assistant.crypto import decrypt, encrypt

        key = b"\x00" * 32
        ct = encrypt(b"secret", key)
        tampered = ct[:-1] + bytes([ct[-1] ^ 0xFF])
        with self.assertRaises(InvalidTag):
            decrypt(tampered, key)

    def test_crypto_wrong_key_fails(self) -> None:
        from cryptography.exceptions import InvalidTag

        from native_chinese_assistant.crypto import decrypt, encrypt

        ct = encrypt(b"secret", b"\x00" * 32)
        with self.assertRaises(InvalidTag):
            decrypt(ct, b"\x01" * 32)

    def test_quality_store_persists_encrypted_when_key_set(self) -> None:
        import base64

        from native_chinese_assistant.crypto import MAGIC
        from native_chinese_assistant.feedback import QualityStore

        os.environ["NCGA_DATA_KEY"] = base64.urlsafe_b64encode(b"\x42" * 32).decode()
        path = Path(tempfile.mkdtemp()) / "store.json"
        s1 = QualityStore(path=path)
        s1.record("v", "sc", 4.0, "原", "改", "")
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

    def test_index_html_injects_meta_tag_when_token_set(self) -> None:
        os.environ["NCGA_AUTH_TOKEN"] = "abc-123"
        app = App(rewrite_service=RewriteService(config=None))
        _, _, body = call_app(app, "GET", "/")
        self.assertIn(b'<meta name="ncga-auth" content="abc-123">', body)

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


if __name__ == "__main__":
    unittest.main()
