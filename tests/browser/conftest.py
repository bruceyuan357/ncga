"""Pytest fixtures for the browser test suite (Cycle 18 v2).

Spawns the WSGI app on an ephemeral port for the duration of one test session.
Auth is disabled and a stub LLM is wired in so tests are hermetic — they don't
hit the real DeepSeek API. The bug-fix tests are pure UI (no LLM calls), but
the fixtures are general so we can add wizard / phrase tests later.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _SilentHandler(WSGIRequestHandler):
    """Suppress per-request stderr lines that clutter the pytest output."""

    def log_message(self, format, *args) -> None:  # noqa: A002, ARG002
        return


@pytest.fixture(scope="session")
def live_app() -> Iterator[str]:
    """Start NCGA on an ephemeral port; yield base URL; clean up after."""
    # Force auth off + a benign quality-store path (XDG isolation)
    os.environ.setdefault("NCGA_AUTH_TOKEN", "")
    os.environ.setdefault("NCGA_DATA_KEY", "")
    import tempfile

    tmp = tempfile.mkdtemp(prefix="ncga-pw-")
    os.environ["XDG_DATA_HOME"] = tmp

    # Inject a fake LLM transport so the wizard / rewrite paths don't hit real network
    from native_chinese_assistant.rewrite import (
        ChatCompletionsClient,
        LLMConfig,
        RewriteService,
    )
    from native_chinese_assistant.web import App

    class FakeTransport:
        def __init__(self) -> None:
            self.body = json.dumps(
                {"choices": [{"message": {"content": json.dumps({"rewritten_text": "好的", "warning": ""})}}]}
            ).encode("utf-8")

        def post(self, url, body, headers, *, timeout, ssl_context):  # noqa: ARG002
            class Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return None

                def read(self_inner):
                    return self.body

            return Resp()

    cfg = LLMConfig(
        provider="deepseek",
        api_key="test",
        model="test-model",
        base_url="https://test.example",
        streaming=False,
        ca_bundle=None,
        skip_ssl_verify=False,
        timeout_seconds=5.0,
    )
    client = ChatCompletionsClient(cfg, transport=FakeTransport())
    app = App(rewrite_service=RewriteService(client=client))

    port = _free_port()
    server = make_server("127.0.0.1", port, app, handler_class=_SilentHandler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    # Wait for readiness
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)

    base_url = f"http://127.0.0.1:{port}"
    yield base_url
    server.shutdown()
    th.join(timeout=2)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
