from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from modelmux.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    require_json_content_type,
)
from modelmux.errors import PayloadTooLargeError, UnsupportedMediaTypeError
from modelmux.observability.logging import request_id_var, setup_logging

LIMIT = 1024


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, str | None]:
        return {"request_id": request_id_var.get()}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    @app.post("/json", dependencies=[Depends(require_json_content_type)])
    async def json_only() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=LIMIT)
    app.add_middleware(RequestContextMiddleware, driver_name="fake")
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    setup_logging("INFO", stream=stream)
    yield stream
    setup_logging("INFO", stream=io.StringIO())


def test_request_id_generated(client: TestClient) -> None:
    resp = client.get("/ok")
    rid = resp.headers["x-request-id"]
    assert rid.startswith("req_")
    assert len(rid) == 28
    assert resp.json()["request_id"] == rid
    assert resp.headers["x-modelmux-driver"] == "fake"


def test_request_id_accepted(client: TestClient) -> None:
    resp = client.get("/ok", headers={"X-Request-ID": "abc.DEF_123-x"})
    assert resp.headers["x-request-id"] == "abc.DEF_123-x"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "semi;colon", "<script>", ""])
def test_request_id_rejected(client: TestClient, bad: str) -> None:
    resp = client.get("/ok", headers={"X-Request-ID": bad})
    assert resp.headers["x-request-id"] != bad
    assert resp.headers["x-request-id"].startswith("req_")


def test_body_under_limit(client: TestClient) -> None:
    resp = client.post("/echo", content=b"x" * LIMIT)
    assert resp.status_code == 200
    assert resp.json() == {"length": LIMIT}


def test_declared_length_over_limit(client: TestClient) -> None:
    resp = client.post("/echo", content=b"x" * (LIMIT + 1))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert resp.headers["x-request-id"]


def test_invalid_content_length_rejected() -> None:
    sent: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        raise AssertionError("must not be called")

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:  # pragma: no cover
        return {"type": "http.request", "body": b""}

    mw = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)  # type: ignore[arg-type]
    scope = {"type": "http", "headers": [(b"content-length", b"nope")]}
    asyncio.run(mw(scope, receive, send))  # type: ignore[arg-type]
    assert sent[0]["status"] == 400


def test_chunked_body_over_limit(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(4):
            yield b"x" * 512

    with pytest.raises(PayloadTooLargeError):
        client.post("/echo", content=chunks())


def test_chunked_body_under_limit(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 100
        yield b"y" * 100

    resp = client.post("/echo", content=chunks())
    assert resp.json() == {"length": 200}


@pytest.mark.parametrize("ctype", ["application/json", "application/json; charset=utf-8"])
def test_json_content_type_ok(client: TestClient, ctype: str) -> None:
    resp = client.post("/json", content=b"{}", headers={"content-type": ctype})
    assert resp.status_code == 200


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", None])
def test_non_json_rejected(client: TestClient, ctype: str | None) -> None:
    headers = {"content-type": ctype} if ctype else {}
    with pytest.raises(UnsupportedMediaTypeError):
        client.post("/json", content=b"{}", headers=headers)


def test_access_log(client: TestClient, log_stream: io.StringIO) -> None:
    client.get("/ok?token=secret-in-query", headers={"X-Request-ID": "trace-1"})
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    access = [line for line in lines if line.get("event") == "access"]
    assert len(access) == 1
    entry = access[0]
    assert entry["status"] == 200
    assert entry["path"] == "/ok"
    assert entry["method"] == "GET"
    assert entry["request_id"] == "trace-1"
    assert entry["driver"] == "fake"
    assert "secret-in-query" not in json.dumps(entry)


def test_access_log_on_unhandled_error(log_stream: io.StringIO) -> None:
    app = build_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        TestClient(app).get("/boom")
    access = [
        json.loads(line)
        for line in log_stream.getvalue().splitlines()
        if '"event": "access"' in line
    ]
    assert access[0]["status"] == 500


def test_non_http_scope_passthrough() -> None:
    calls: list[str] = []

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        calls.append(str(scope["type"]))

    for mw in (
        RequestContextMiddleware(app, driver_name="x"),  # type: ignore[arg-type]
        BodySizeLimitMiddleware(app, max_body_bytes=1),  # type: ignore[arg-type]
    ):
        asyncio.run(mw({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    assert calls == ["lifespan", "lifespan"]


def test_logging_quiet_by_default() -> None:
    assert logging.getLogger("modelmux.access").level in (logging.NOTSET, logging.INFO)
