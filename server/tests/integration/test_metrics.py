from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import AUTH, assert_openai_error, chat_body, use_claude_fixture


def test_metrics_disabled_by_default(client: TestClient) -> None:
    assert_openai_error(client.get("/metrics", headers=AUTH), 404, "not_found",
                        "invalid_request_error")  # fmt: skip


def test_metrics_enabled(make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(make_app(enable_metrics=True), raise_server_exceptions=False) as c:
        assert c.get("/metrics").status_code == 401
        c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
        c.post("/v1/chat/completions", json=chat_body(model="evil-high-cardinality"),
               headers=AUTH)  # fmt: skip
        use_claude_fixture(monkeypatch, "tool_use_read.jsonl")
        c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
        resp = c.get("/metrics", headers=AUTH)
    assert resp.status_code == 200
    text = resp.text
    assert 'modelmux_chat_requests_total{model="sonnet",outcome="ok"} 1.0' in text
    assert 'modelmux_chat_requests_total{model="sonnet",outcome="sandbox_violation"} 1.0' in text
    assert 'outcome="model_not_found"' in text
    assert "evil-high-cardinality" not in text
    assert "modelmux_active_processes 0.0" in text
    assert "modelmux_queued_requests 0.0" in text
