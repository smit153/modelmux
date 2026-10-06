"""The full API with the Codex driver (fake CLI)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.helpers import AUTH, assert_openai_error, chat_body, use_fixture

URL = "/v1/chat/completions"


@pytest.fixture
def driver_name() -> str:
    return "codex"


def test_chat_ok(client: TestClient) -> None:
    resp = client.post(URL, json=chat_body(driver="codex"), headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "gpt-6.1-sol"
    assert data["choices"][0]["message"]["content"] == "Hello from fake Codex."
    assert data["system_fingerprint"].endswith("-codex")
    # input_tokens=15 already includes 3 cached + 2 cache-write tokens.
    assert data["usage"]["prompt_tokens"] == 15
    assert data["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
    assert data["usage"]["completion_tokens"] == 5
    assert resp.headers["x-modelmux-driver"] == "codex"


def test_models(client: TestClient) -> None:
    ids = [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]]
    # Exactly what the CLI's catalog lists as visible, by priority.
    assert ids == [
        "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    ]  # fmt: skip


def test_claude_model_not_available(client: TestClient) -> None:
    resp = client.post(URL, json=chat_body(driver="claude"), headers=AUTH)
    assert_openai_error(resp, 404, "model_not_found", "invalid_request_error")


@pytest.mark.parametrize(
    ("fixture", "exit_code", "status", "code"),
    [
        ("auth_error.jsonl", 1, 502, "provider_auth_error"),
        ("rate_limited.jsonl", 1, 429, "provider_rate_limited"),
        ("usage_limit.jsonl", 1, 429, "provider_rate_limited"),
        ("context_too_long.jsonl", 1, 413, "context_too_large"),
        ("overloaded.jsonl", 1, 503, "provider_unavailable"),
        ("model_not_found.jsonl", 1, 404, "model_not_found"),
        ("no_completion.jsonl", 0, 502, "protocol_error"),
        ("malformed.jsonl", 0, 502, "protocol_error"),
        ("file_change.jsonl", 0, 502, "sandbox_violation"),
        ("web_search.jsonl", 0, 502, "sandbox_violation"),
    ],
)
def test_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    exit_code: int,
    status: int,
    code: str,
) -> None:
    use_fixture(monkeypatch, "codex", fixture, exit_code)
    resp = client.post(URL, json=chat_body(driver="codex"), headers=AUTH)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert "api.openai.com" not in resp.text
