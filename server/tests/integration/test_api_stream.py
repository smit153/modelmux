from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmux.main import _startup_checks
from tests.fakes import install_fake_cli
from tests.helpers import AUTH, assert_openai_error, chat_body, use_claude_fixture
from tests.proc import alive, group_members

URL = "/v1/chat/completions"


def events(text: str) -> list[Any]:
    """Parse an SSE body into JSON objects (and the literal "[DONE]")."""
    out: list[Any] = []
    for block in text.split("\n\n"):
        if block.startswith("data: "):
            data = block.removeprefix("data: ")
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def content(chunks: list[Any]) -> str:
    return "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if isinstance(c, dict) and c.get("choices")
    )


def test_stream_ok(client: TestClient) -> None:
    resp = client.post(URL, json=chat_body(stream=True, temperature=0), headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-modelmux-ignored-params"] == "temperature"
    assert resp.headers["x-request-id"]
    chunks = events(resp.text)
    assert chunks[-1] == "[DONE]"
    data = chunks[:-1]
    assert data[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert content(data) == "Hello from fake Claude."
    assert len(data) == 4  # role, two content deltas, finish
    assert data[-1]["choices"][0]["finish_reason"] == "stop"
    assert len({c["id"] for c in data}) == 1
    assert all(c["object"] == "chat.completion.chunk" and "usage" not in c for c in data)


def test_stream_include_usage(client: TestClient) -> None:
    body = chat_body(stream=True, stream_options={"include_usage": True})
    chunks = events(client.post(URL, json=body, headers=AUTH).text)
    *data, usage, done = chunks
    assert done == "[DONE]"
    assert usage["choices"] == []
    assert usage["usage"]["total_tokens"] == 20
    assert all(c["usage"] is None for c in data)


def test_stream_stop_sequence(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_REPLY", "alpha beta STOP gamma")
    chunks = events(client.post(URL, json=chat_body(stream=True, stop="STOP"), headers=AUTH).text)
    assert content(chunks[:-1]) == "alpha beta "
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize(
    ("fixture", "exit_code", "status", "code"),
    [
        ("rate_limited.jsonl", 1, 429, "provider_rate_limited"),
        ("tool_use_read.jsonl", 0, 502, "sandbox_violation"),
        ("malformed.jsonl", 0, 502, "protocol_error"),
    ],
)
def test_errors_before_first_chunk_are_http_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    exit_code: int,
    status: int,
    code: str,
) -> None:
    use_claude_fixture(monkeypatch, fixture, exit_code)
    resp = client.post(URL, json=chat_body(stream=True), headers=AUTH)
    err = assert_openai_error(resp, status, code, resp.json()["error"]["type"])
    assert err["code"] == code


def write_fixture(tmp_path: Path, name: str, lines: list[dict[str, Any]]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        ({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 529,
          "result": "Overloaded"}, "provider_unavailable"),
        ({"type": "stream_event", "event": {"type": "content_block_start",
          "content_block": {"type": "tool_use", "name": "Bash"}}}, "sandbox_violation"),
    ],
)  # fmt: skip
def test_errors_after_first_chunk_become_error_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tail: dict[str, Any],
    code: str,
) -> None:
    fixture = write_fixture(tmp_path, "mid.jsonl", [delta("partial "), tail])
    monkeypatch.setenv("FAKE_SCENARIO", "fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    resp = client.post(URL, json=chat_body(stream=True), headers=AUTH)
    assert resp.status_code == 200
    chunks = events(resp.text)
    assert content(chunks[:-2]) == "partial "
    assert chunks[-2]["error"]["code"] == code
    assert chunks[-1] == "[DONE]"
    assert "Bash" not in resp.text
    assert list(client.app.state.settings.work_root.iterdir()) == []  # type: ignore[attr-defined]


def test_stream_codex_single_chunk(make_app: Callable[..., FastAPI], tmp_path: Path) -> None:
    app = make_app(driver="codex", cli_path=install_fake_cli(tmp_path / "cx", "codex"))
    with TestClient(app) as c:
        resp = c.post(URL, json=chat_body(stream=True, driver="codex"), headers=AUTH)
    chunks = events(resp.text)
    assert content(chunks[:-1]) == "Hello from fake Codex."
    assert len(chunks) == 4  # role, one content chunk, finish, [DONE]


# ------------------------------------------------------------------ disconnect


async def drive_raw(app: FastAPI, body: dict[str, Any], disconnect: asyncio.Event) -> list[Any]:
    """Call the ASGI app directly; ``disconnect`` makes the client go away."""
    raw = json.dumps(body).encode()
    sent: list[Any] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
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
                    (b"content-length", str(len(raw)).encode())],
    }  # fmt: skip
    await asyncio.wait_for(app(scope, receive, send), 10)
    return sent


async def test_disconnect_mid_stream_kills(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = make_app()
    await _startup_checks(app)
    out = tmp_path / "rec.json"
    fixture = write_fixture(tmp_path, "slow.jsonl", [delta("first ")])
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("FAKE_SCENARIO", "fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    monkeypatch.setenv("FAKE_HANG_AFTER", "1")  # streams one delta, then hangs

    disconnect = asyncio.Event()
    call = asyncio.create_task(drive_raw(app, chat_body(stream=True), disconnect))
    while not out.exists():
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)  # let the first chunk reach the client
    disconnect.set()
    sent = await call

    assert sent[0]["status"] == 200
    assert b"first " in b"".join(m.get("body", b"") for m in sent[1:])
    pid = json.loads(out.read_text())["pid"]
    assert not alive(pid)
    assert group_members(pid) == []
    assert list(app.state.settings.work_root.iterdir()) == []
    assert app.state.limiter.active == 0


async def test_disconnect_before_first_chunk(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = make_app()
    await _startup_checks(app)
    out = tmp_path / "rec.json"
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("FAKE_SCENARIO", "hang_before_output")
    disconnect = asyncio.Event()
    call = asyncio.create_task(drive_raw(app, chat_body(stream=True), disconnect))
    while not out.exists():
        await asyncio.sleep(0.02)
    disconnect.set()
    sent = await call
    assert sent[0]["status"] == 499
    pid = json.loads(out.read_text())["pid"]
    assert not alive(pid)
    assert app.state.limiter.active == 0
