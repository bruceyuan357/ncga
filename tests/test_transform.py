"""Cycle 23: transform-mode (polish/translate/summarize/explain) contract tests.

Covers mode parsing, env-driven model routing (explain → deepseek-v4-pro on the
deepseek provider), payload construction, the four web endpoints, auth gating,
and quality-store recording under (mode:<key>, "transform") buckets.
"""

from __future__ import annotations

import json
import os
import unittest

from test_app import (
    FakeTransport,
    _build_client,
    _llm_json_raw,
    call_app,
)

from native_chinese_assistant.feedback import QualityStore
from native_chinese_assistant.rewrite import (
    ChatCompletionsClient,
    LLMConfig,
    RewriteError,
    RewriteService,
    load_llm_config,
)
from native_chinese_assistant.transform import (
    MAX_TRANSFORM_OUTPUT_CHARS,
    MODE_METADATA,
    TransformMode,
    TransformService,
    parse_mode,
)
from native_chinese_assistant.web import App


def setUpModule():
    os.environ["NCGA_AUTH_TOKEN"] = ""
    os.environ["NCGA_DATA_KEY"] = ""


# ---------------- helpers ----------------


def _client_with_overrides(
    response_body: bytes,
    overrides: dict[str, str],
    *,
    streaming: bool = False,
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
        model_overrides=overrides,
    )
    transport = FakeTransport(response_body)
    return ChatCompletionsClient(config, transport=transport), transport


def _make_app(client: ChatCompletionsClient) -> App:
    return App(rewrite_service=RewriteService(client=client), quality_store=QualityStore())


def _sse_stream_body(*chunks: str) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": c}}]}, ensure_ascii=False) for c in chunks
    ]
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------- mode parsing ----------------


class ParseModeTests(unittest.TestCase):
    def test_all_four_modes_parse(self):
        for raw in ("polish", "translate", "summarize", "explain"):
            self.assertEqual(parse_mode(raw).value, raw)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            parse_mode("poem")

    def test_metadata_complete(self):
        for mode in TransformMode:
            meta = MODE_METADATA[mode]
            self.assertTrue(meta.label)
            self.assertTrue(meta.system_prompt)
            self.assertGreater(meta.max_tokens, 0)


# ---------------- model routing (load_llm_config) ----------------


class ModelRoutingConfigTests(unittest.TestCase):
    _KEYS = ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL_EXPLAIN", "LLM_MODEL_SUMMARIZE")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_PROVIDER"] = "deepseek"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_explain_defaults_to_pro_on_deepseek(self):
        # Local .env may set the same value via setdefault; either path must
        # land on the pro tier — that's the contract the user decided on.
        os.environ.pop("LLM_MODEL_EXPLAIN", None)
        config = load_llm_config()
        self.assertIsNotNone(config)
        self.assertEqual(config.model_overrides.get("explain"), "deepseek-v4-pro")

    def test_env_override_beats_default(self):
        os.environ["LLM_MODEL_EXPLAIN"] = "my-custom-pro"
        config = load_llm_config()
        self.assertEqual(config.model_overrides.get("explain"), "my-custom-pro")

    def test_generic_scan_picks_up_any_mode(self):
        os.environ["LLM_MODEL_SUMMARIZE"] = "sum-model"
        config = load_llm_config()
        self.assertEqual(config.model_overrides.get("summarize"), "sum-model")

    def test_non_deepseek_provider_gets_no_explain_default(self):
        os.environ["LLM_PROVIDER"] = "openai"
        # Blank disables: the scan skips empty values and setdefault won't
        # resurrect the .env line over an existing (empty) env var.
        os.environ["LLM_MODEL_EXPLAIN"] = ""
        config = load_llm_config()
        self.assertIsNone(config.model_overrides.get("explain"))


# ---------------- service ----------------


class TransformServiceTests(unittest.TestCase):
    def test_polish_uses_global_model_and_plain_text_format(self):
        client, transport = _build_client(_llm_json_raw("润色后的句子。"))
        service = TransformService(client=client)
        result = service.transform("这个句子有点儿别扭吧", TransformMode.POLISH)
        self.assertEqual(result.transformed_text, "润色后的句子。")
        self.assertEqual(result.model, "test-model")
        self.assertFalse(result.degraded)
        body = json.loads(transport.calls[-1]["body"])
        self.assertEqual(body["model"], "test-model")
        self.assertNotIn("response_format", body)  # plain prose, no JSON envelope
        self.assertIn("润色", body["messages"][0]["content"])
        self.assertIn("这个句子有点儿别扭吧", body["messages"][1]["content"])

    def test_explain_routes_to_override_model(self):
        client, transport = _client_with_overrides(
            _llm_json_raw("这句话的意思是……"), {"explain": "deepseek-v4-pro"}
        )
        service = TransformService(client=client)
        result = service.transform("不可抗力条款", TransformMode.EXPLAIN)
        self.assertEqual(result.model, "deepseek-v4-pro")
        body = json.loads(transport.calls[-1]["body"])
        self.assertEqual(body["model"], "deepseek-v4-pro")

    def test_other_modes_unaffected_by_explain_override(self):
        client, transport = _client_with_overrides(_llm_json_raw("summary"), {"explain": "deepseek-v4-pro"})
        TransformService(client=client).transform("hello world, long text", TransformMode.SUMMARIZE)
        body = json.loads(transport.calls[-1]["body"])
        self.assertEqual(body["model"], "test-model")

    def test_empty_llm_result_raises(self):
        client, _ = _build_client(_llm_json_raw(""))
        with self.assertRaises(RewriteError):
            TransformService(client=client).transform("some text here", TransformMode.POLISH)

    def test_no_client_raises(self):
        client, _ = _build_client(_llm_json_raw("x"))
        service = TransformService(client=client)
        service._client = None
        with self.assertRaises(RewriteError):
            service.transform("some text here", TransformMode.POLISH)

    def test_overlong_output_truncated_with_warning(self):
        client, _ = _build_client(_llm_json_raw("长" * (MAX_TRANSFORM_OUTPUT_CHARS + 500)))
        result = TransformService(client=client).transform("text", TransformMode.EXPLAIN)
        self.assertEqual(len(result.transformed_text), MAX_TRANSFORM_OUTPUT_CHARS)
        self.assertTrue(result.warning)

    def test_rate_parses_judge_json(self):
        client, transport = _build_client(_llm_json_raw('{"score": 87, "reason": "贴切"}'))
        data = TransformService(client=client).rate("译文", TransformMode.TRANSLATE, original="原文")
        self.assertEqual(data, {"score": 87, "reason": "贴切"})
        body = json.loads(transport.calls[-1]["body"])
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_rate_clamps_score_into_range(self):
        client, _ = _build_client(_llm_json_raw('{"score": 250, "reason": "x"}'))
        data = TransformService(client=client).rate("y", TransformMode.POLISH)
        self.assertEqual(data["score"], 100)

    def test_stream_yields_4tuple_with_model_in_done_meta(self):
        client, transport = _client_with_overrides(
            _sse_stream_body("你", "好"), {"explain": "pro-x"}, streaming=True
        )
        events = list(TransformService(client=client).transform_stream("explain this", TransformMode.EXPLAIN))
        self.assertEqual(events[0], ("你", "你", False, None))
        self.assertEqual(events[1], ("好", "你好", False, None))
        delta, partial, done, meta = events[-1]
        self.assertTrue(done)
        self.assertEqual(partial, "你好")
        self.assertEqual(meta, {"degraded": False, "warning": None, "model": "pro-x"})
        body = json.loads(transport.calls[-1]["body"])
        self.assertEqual(body["model"], "pro-x")


# ---------------- web endpoints ----------------


class TransformEndpointTests(unittest.TestCase):
    def test_transform_happy_path(self):
        app = _make_app(_build_client(_llm_json_raw("更通顺的句子。"))[0])
        status, _, body = call_app(
            app, "POST", "/api/transform", json.dumps({"text": "句子别扭", "mode": "polish"}).encode()
        )
        self.assertEqual(status, "200 OK")
        data = json.loads(body)
        self.assertEqual(data["transformed_text"], "更通顺的句子。")
        self.assertEqual(data["mode"], "polish")
        self.assertEqual(data["model"], "test-model")
        self.assertFalse(data["degraded"])

    def test_missing_mode_is_400_with_field_name(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        status, _, body = call_app(app, "POST", "/api/transform", json.dumps({"text": "abc"}).encode())
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("'mode'", json.loads(body)["error"])

    def test_unknown_mode_is_400(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        status, _, body = call_app(
            app, "POST", "/api/transform", json.dumps({"text": "abc", "mode": "poem"}).encode()
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Unsupported transform mode", json.loads(body)["error"])

    def test_non_string_text_is_400(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        status, _, body = call_app(
            app, "POST", "/api/transform", json.dumps({"text": 42, "mode": "polish"}).encode()
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("`text` must be a string", json.loads(body)["error"])

    def test_requires_auth_when_token_set(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        os.environ["NCGA_AUTH_TOKEN"] = "secret-token-for-test"
        try:
            status, _, _ = call_app(
                app, "POST", "/api/transform", json.dumps({"text": "a", "mode": "polish"}).encode()
            )
            self.assertEqual(status, "401 Unauthorized")
        finally:
            os.environ["NCGA_AUTH_TOKEN"] = ""

    def test_503_when_no_llm_configured(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        app.transform_service._client = None
        status, _, body = call_app(
            app, "POST", "/api/transform", json.dumps({"text": "abc", "mode": "polish"}).encode()
        )
        self.assertEqual(status, "503 Service Unavailable")
        self.assertIn("No LLM provider", json.loads(body)["error"])

    def test_transform_modes_listing(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        status, _, body = call_app(app, "GET", "/api/transform-modes")
        self.assertEqual(status, "200 OK")
        modes = json.loads(body)["modes"]
        self.assertEqual([m["key"] for m in modes], ["polish", "translate", "summarize", "explain"])
        self.assertEqual(modes[0]["label"], "润色")

    def test_stream_endpoint_emits_chunks_and_done_with_model(self):
        client, _ = _client_with_overrides(_sse_stream_body("你", "好"), {"explain": "pro-x"}, streaming=True)
        app = _make_app(client)
        status, headers, body = call_app(
            app,
            "POST",
            "/api/transform-stream",
            json.dumps({"text": "explain this", "mode": "explain"}).encode(),
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("text/event-stream", headers["Content-Type"])
        text = body.decode("utf-8")
        self.assertIn("event: chunk", text)
        done_data = None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line == "event: done":
                done_data = json.loads(lines[i + 1].removeprefix("data: "))
        self.assertIsNotNone(done_data)
        self.assertEqual(done_data["transformed_text"], "你好")
        self.assertEqual(done_data["mode"], "explain")
        self.assertEqual(done_data["model"], "pro-x")
        self.assertFalse(done_data["degraded"])

    def test_rate_transform_records_mode_bucket(self):
        app = _make_app(_build_client(_llm_json_raw('{"score": 87, "reason": "贴切"}'))[0])
        status, _, body = call_app(
            app,
            "POST",
            "/api/rate-transform",
            json.dumps({"transformed": "好句子", "mode": "polish", "original": "句子"}).encode(),
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["score"], 87)
        bucket = app.quality_store._buckets[("mode:polish", "transform")]
        self.assertEqual(len(bucket.samples), 1)
        self.assertEqual(bucket.stats.n, 1)

    def test_rate_transform_missing_field_is_400(self):
        app = _make_app(_build_client(_llm_json_raw("x"))[0])
        status, _, body = call_app(
            app, "POST", "/api/rate-transform", json.dumps({"mode": "polish"}).encode()
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("'transformed'", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
