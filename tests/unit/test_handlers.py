from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from modelmux.api.handlers import register_exception_handlers
from modelmux.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    require_json_content_type,
)
from modelmux.errors import OverloadedError, SandboxViolationError
from modelmux.observability.logging import setup_logging
from tests.helpers import assert_openai_error

SENTINEL = "SENTINEL_stderr_/opt/secret/path_argv"


class Body(BaseModel):
    name: str
    count: int


def build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/violation")
    async def violation() -> None:
        raise SandboxViolationError(f"tool_use Bash {SENTINEL}")

    @app.get("/busy")
    async def busy() -> None:
        raise OverloadedError("queue full", retry_after=5)

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError(SENTINEL)

    @app.post("/typed", dependencies=[Depends(require_json_content_type)])
    async def typed(body: Body) -> dict[str, str]:
        return {"name": body.name}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=128)
    app.add_middleware(RequestContextMiddleware, driver_name="fake")
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    setup_logging("INFO", stream=stream)
    yield stream
    setup_logging("INFO", stream=io.StringIO())


def test_modelmux_error(client: TestClient, log_stream: io.StringIO) -> None:
    resp = client.get("/violation")
    assert_openai_error(resp, 502, "sandbox_violation", "server_error", forbidden=(SENTINEL,))
    assert SENTINEL in log_stream.getvalue()  # detail is logged, not returned


def test_retry_after(client: TestClient) -> None:
    resp = client.get("/busy")
    assert_openai_error(resp, 503, "overloaded", "server_error", forbidden=(SENTINEL,))
    assert resp.headers["retry-after"] == "5"


def test_unexpected_exception(client: TestClient, log_stream: io.StringIO) -> None:
    resp = client.get("/crash", headers={"X-Request-ID": "crash-1"})
    err = assert_openai_error(resp, 500, "internal_error", "server_error", forbidden=(SENTINEL,))
    assert err["message"] == "An internal error occurred."
    assert resp.headers["x-request-id"] == "crash-1"
    logs = log_stream.getvalue()
    assert "unhandled_exception" in logs
    assert "Traceback" in logs


def test_validation_error_names_field_without_value(client: TestClient) -> None:
    resp = client.post("/typed", json={"name": "ok", "count": f"not-int-{SENTINEL}"})
    err = assert_openai_error(
        resp, 400, "invalid_request", "invalid_request_error", forbidden=(SENTINEL,)
    )
    assert err["param"] == "count"
    assert "count" in str(err["message"])


def test_invalid_json(client: TestClient) -> None:
    resp = client.post("/typed", content=b'{"name": ', headers={"content-type": "application/json"})
    err = assert_openai_error(
        resp, 400, "invalid_request", "invalid_request_error", forbidden=(SENTINEL,)
    )
    assert "JSON" in str(err["message"])


def test_unknown_route(client: TestClient) -> None:
    assert_openai_error(
        client.get("/nope"), 404, "not_found", "invalid_request_error", forbidden=(SENTINEL,)
    )


def test_wrong_method(client: TestClient) -> None:
    assert_openai_error(
        client.get("/typed"),
        405,
        "method_not_allowed",
        "invalid_request_error",
        forbidden=(SENTINEL,),
    )


def test_unsupported_media_type(client: TestClient) -> None:
    resp = client.post("/typed", content=b"{}", headers={"content-type": "text/plain"})
    assert_openai_error(
        resp, 415, "unsupported_media_type", "invalid_request_error", forbidden=(SENTINEL,)
    )


def test_streamed_body_over_limit(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 100
        yield b"x" * 100

    resp = client.post("/echo", content=chunks())
    assert_openai_error(
        resp, 413, "payload_too_large", "invalid_request_error", forbidden=(SENTINEL,)
    )
