"""Pure ASGI middleware: request context, body size limit, access log.

These are plain ASGI callables rather than Starlette ``BaseHTTPMiddleware``
subclasses, which buffer streaming responses and interfere with client
disconnect detection.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import Request

from modelmux.errors import (
    InvalidRequestError,
    ModelMuxError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from modelmux.observability.logging import driver_var, request_id_var

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
DRIVER_HEADER = "X-ModelMux-Driver"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

access_log = logging.getLogger("modelmux.access")


def new_request_id() -> str:
    return "req_" + secrets.token_hex(12)


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return bytes(value)
    return None


def get_request_id(scope: Scope) -> str | None:
    """The request ID assigned by ``RequestContextMiddleware``, if any."""
    state = scope.get("state") or {}
    value = state.get("request_id")
    return value if isinstance(value, str) else None


async def send_error(send: Send, err: ModelMuxError, request_id: str | None) -> None:
    """Send an OpenAI-shaped error directly from middleware."""
    body = json.dumps(err.to_openai()).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    headers += [(k.lower().encode(), v.encode()) for k, v in err.headers().items()]
    if request_id:
        headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
    await send({"type": "http.response.start", "status": err.http_status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestContextMiddleware:
    """Assigns the request ID, sets log context, adds headers, writes the access log."""

    def __init__(self, app: ASGIApp, *, driver_name: str) -> None:
        self.app = app
        self.driver_name = driver_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = _header(scope, b"x-request-id")
        candidate = supplied.decode("latin-1") if supplied else ""
        request_id = candidate if _REQUEST_ID_RE.fullmatch(candidate) else new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        rid_token = request_id_var.set(request_id)
        drv_token = driver_var.set(self.driver_name)
        started = time.perf_counter()
        status = 0
        response_bytes = 0
        extra_headers = [
            (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
            (DRIVER_HEADER.lower().encode(), self.driver_name.encode()),
        ]

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {b"x-request-id", b"x-modelmux-driver", b"server"}
                ]
                message = {**message, "headers": headers + extra_headers}
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The catch-all handler has already sent a 500 response.
            status = status or 500
            raise
        finally:
            access_log.info(
                "request finished",
                extra={
                    "event": "access",
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "response_bytes": response_bytes,
                },
            )
            driver_var.reset(drv_token)
            request_id_var.reset(rid_token)


class BodySizeLimitMiddleware:
    """Caps request bodies before any parsing, including chunked bodies.

    A declared ``Content-Length`` over the limit is rejected immediately. The
    actual bytes received are also counted, so chunked or lying clients are
    stopped as soon as the limit is crossed.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                declared_len = int(declared)
            except ValueError:
                declared_len = -1
            if declared_len < 0:
                await send_error(send, InvalidRequestError(), get_request_id(scope))
                return
            if declared_len > self.max_body_bytes:
                await send_error(send, PayloadTooLargeError(), get_request_id(scope))
                return

        received = 0
        limit = self.max_body_bytes

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise PayloadTooLargeError(f"body exceeded {limit} bytes while streaming")
            return message

        await self.app(scope, limited_receive, send)


async def require_json_content_type(request: Request) -> None:
    """FastAPI dependency: POST bodies must be ``application/json``.

    Used as a route dependency (after authentication) rather than middleware,
    so unauthenticated callers always get 401 first.
    """
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise UnsupportedMediaTypeError()
