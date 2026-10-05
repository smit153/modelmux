from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmux.main import _startup_checks
from tests.conftest import TEST_API_KEY
from tests.helpers import AUTH, assert_openai_error, chat_body, use_claude_fixture
from tests.proc import group_members, wait_dead

URL = "/v1/chat/completions"


def test_chat_ok(client: TestClient) -> None:
    resp = client.post(URL, json=chat_body(), headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["id"].startswith("chatcmpl-")
    assert data["model"] == "sonnet"
    assert data["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from fake Claude."},
            "finish_reason": "stop",
            "logprobs": None,
        }
    ]
    assert data["usage"]["total_tokens"] == 20
    assert data["system_fingerprint"].startswith("modelmux-")
    assert resp.headers["x-request-id"]
    assert resp.headers["x-modelmux-driver"] == "claude"
    assert "x-modelmux-usage" not in resp.headers
    assert "x-modelmux-ignored-params" not in resp.headers


def test_ignored_params_header(client: TestClient) -> None:
    body = chat_body(temperature=0.1, max_tokens=5, user="u", future_param=1)
    resp = client.post(URL, json=body, headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["x-modelmux-ignored-params"] == "temperature,max_tokens,user"
    assert "future_param" not in resp.text


def test_usage_unavailable_header(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NO_USAGE", "1")
    resp = client.post(URL, json=chat_body(), headers=AUTH)
    assert resp.headers["x-modelmux-usage"] == "unavailable"
    assert resp.json()["usage"]["total_tokens"] == 0


# ------------------------------------------------------------------ request order


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({}, b"{}"),
        ({"Authorization": "Bearer wrong"}, b"{}"),
        ({"Content-Type": "text/plain"}, b"not json"),
        ({"Content-Type": "application/json"}, b"{not json"),
    ],
)
def test_auth_checked_first(client: TestClient, headers: dict[str, str], content: bytes) -> None:
    resp = client.post(URL, content=content, headers=headers)
    assert_openai_error(resp, 401, "authentication_failed", "authentication_error")


def test_non_json_content_type(client: TestClient) -> None:
    resp = client.post(URL, content=b"{}", headers={**AUTH, "Content-Type": "text/plain"})
    assert_openai_error(resp, 415, "unsupported_media_type", "invalid_request_error")


@pytest.mark.parametrize("content", [b"{not json", b"[1,2]", b'"x"', b"\xff\xfe"])
def test_bad_json(client: TestClient, content: bytes) -> None:
    resp = client.post(URL, content=content, headers={**AUTH, "Content-Type": "application/json"})
    assert_openai_error(resp, 400, "invalid_request", "invalid_request_error")


def test_schema_error_names_field(client: TestClient) -> None:
    resp = client.post(
        URL, json={"model": "sonnet", "messages": [{"content": "SECRET-VALUE"}]}, headers=AUTH
    )
    err = assert_openai_error(
        resp, 400, "invalid_request", "invalid_request_error", forbidden=("SECRET-VALUE",)
    )
    assert err["param"] == "messages.0.role"


@pytest.mark.parametrize(
    ("body", "status", "code", "param"),
    [
        (chat_body(model="gpt-4o"), 404, "model_not_found", "model"),
        (chat_body(n=3), 400, "invalid_request", "n"),
        (chat_body(functions=[{"name": "f"}]), 400, "unsupported_parameter", "functions"),
        ({"model": "sonnet", "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}}]}]}, 400, "unsupported_content",
         "messages.0.content.0"),
    ],
)  # fmt: skip
def test_request_errors(
    client: TestClient, body: dict[str, Any], status: int, code: str, param: str
) -> None:
    err = assert_openai_error(
        client.post(URL, json=body, headers=AUTH), status, code, "invalid_request_error"
    )
    assert err["param"] == param


# ------------------------------------------------------------------ provider errors


@pytest.mark.parametrize(
    ("fixture", "exit_code", "status", "code", "otype"),
    [
        ("rate_limited.jsonl", 1, 429, "provider_rate_limited", "rate_limit_error"),
        ("auth_error.jsonl", 1, 502, "provider_auth_error", "server_error"),
        ("overloaded.jsonl", 1, 503, "provider_unavailable", "server_error"),
        ("context_too_long.jsonl", 1, 413, "context_too_large", "invalid_request_error"),
        ("malformed.jsonl", 0, 502, "protocol_error", "server_error"),
        ("tool_use_read.jsonl", 0, 502, "sandbox_violation", "server_error"),
    ],
)
def test_provider_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    exit_code: int,
    status: int,
    code: str,
    otype: str,
) -> None:
    use_claude_fixture(monkeypatch, fixture, exit_code)
    resp = client.post(URL, json=chat_body(), headers=AUTH)
    assert_openai_error(resp, status, code, otype, forbidden=("Rate limit reached", "/login"))


def test_rate_limit_retry_after(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    use_claude_fixture(monkeypatch, "rate_limit_rejected.jsonl", 1)
    resp = client.post(URL, json=chat_body(), headers=AUTH)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3600"


def test_timeout_is_504(make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", "hang_before_output")
    with TestClient(make_app(first_output_timeout=0.3), raise_server_exceptions=False) as c:
        monkeypatch.setenv("FAKE_SCENARIO", "hang_before_output")
        resp = c.post(URL, json=chat_body(), headers=AUTH)
    assert_openai_error(resp, 504, "provider_timeout", "server_error")


# ------------------------------------------------------------------ models


def test_models(client: TestClient) -> None:
    resp = client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert ids[:4] == ["sonnet", "opus", "haiku", "fable"]
    assert all(m["object"] == "model" and m["owned_by"] == "modelmux-claude" for m in data["data"])


def test_models_requires_auth(client: TestClient) -> None:
    resp = client.get("/v1/models")
    assert_openai_error(resp, 401, "authentication_failed", "authentication_error")


# ------------------------------------------------------------------ concurrency & disconnect


async def test_queue_full_returns_503(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(max_concurrent_processes=1, max_queue_size=0)
    await _startup_checks(app)
    monkeypatch.setenv("FAKE_REPLY", "slow")
    monkeypatch.setenv("FAKE_SCENARIO", "hang_mid_stream")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        slow = asyncio.create_task(ac.post(URL, json=chat_body(), headers=AUTH, timeout=30))
        while app.state.limiter.active == 0:
            await asyncio.sleep(0.02)
        resp = await ac.post(URL, json=chat_body(), headers=AUTH)
        assert_openai_error(resp, 503, "overloaded", "server_error")
        assert resp.headers["retry-after"] == "5"
        assert (await ac.get("/health/ready")).json()["reason"] == "saturated"
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)


async def test_client_disconnect_kills_process(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = make_app()
    await _startup_checks(app)
    out = tmp_path / "rec.json"
    monkeypatch.setenv("FAKE_SCENARIO", "hang_before_output")
    monkeypatch.setenv("FAKE_OUT", str(out))

    body = json.dumps(chat_body()).encode()
    disconnect = asyncio.Event()
    sent: list[dict[str, Any]] = []
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": URL, "raw_path": URL.encode(), "query_string": b"",
        "root_path": "", "server": ("t", 80), "client": ("c", 1),
        "headers": [(b"authorization", AUTH["Authorization"].encode()),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    }  # fmt: skip
    call = asyncio.create_task(app(scope, receive, send))
    while not out.exists():
        await asyncio.sleep(0.02)
    pid = json.loads(out.read_text())["pid"]
    started = time.monotonic()
    disconnect.set()
    await asyncio.wait_for(call, 5)
    assert time.monotonic() - started < 2
    assert sent[0]["status"] == 499
    assert wait_dead(pid, 1)
    assert group_members(pid) == []
    assert list(app.state.settings.work_root.iterdir()) == []


def test_bearer_with_other_key_allowed(make_app: Callable[..., FastAPI]) -> None:
    other = "k" * 40
    with TestClient(make_app(api_keys=f"{TEST_API_KEY},{other}")) as c:
        resp = c.post(URL, json=chat_body(), headers={"Authorization": f"Bearer {other}"})
    assert resp.status_code == 200


def test_deeply_nested_body_is_400(client: TestClient) -> None:
    body = b'{"model":"sonnet","messages":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    resp = client.post(URL, content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert_openai_error(resp, 400, "invalid_request", "invalid_request_error")
