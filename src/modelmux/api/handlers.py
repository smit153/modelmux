"""The single place where errors become HTTP responses.

Every response body has the OpenAI shape
``{"error": {"message", "type", "code", "param"}}`` and carries the
``X-Request-ID`` header. Internal details go to the logs only.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from modelmux.api.middleware import REQUEST_ID_HEADER, get_request_id
from modelmux.errors import (
    AuthenticationFailedError,
    InternalError,
    InvalidRequestError,
    MethodNotAllowedError,
    ModelMuxError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)

log = logging.getLogger("modelmux.errors")

_HTTP_STATUS_ERRORS: dict[int, type[ModelMuxError]] = {
    401: AuthenticationFailedError,
    404: NotFoundError,
    405: MethodNotAllowedError,
    413: PayloadTooLargeError,
    415: UnsupportedMediaTypeError,
}


def error_response(err: ModelMuxError, request: Request) -> JSONResponse:
    """Render ``err`` as an OpenAI-shaped JSON response."""
    headers = err.headers()
    request_id = get_request_id(request.scope)
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(err.to_openai(), status_code=err.http_status, headers=headers)


def log_error(err: ModelMuxError) -> None:
    level = logging.ERROR if err.http_status >= 500 else logging.INFO
    extra: dict[str, Any] = {"event": "request_error", "code": err.code, "status": err.http_status}
    if err.internal_detail:
        extra["detail"] = err.internal_detail[:2000]
    log.log(level, "request failed", extra=extra)


def _field_path(loc: tuple[Any, ...]) -> str:
    parts = [str(part) for part in loc]
    if parts and parts[0] in {"body", "query", "path", "header"}:
        parts = parts[1:]
    return ".".join(parts)


async def handle_modelmux_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ModelMuxError)  # noqa: S101 - registered for this type only
    log_error(exc)
    return error_response(exc, request)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    errors = exc.errors()
    first = errors[0] if errors else {}
    path = _field_path(tuple(first.get("loc", ())))
    if first.get("type") == "json_invalid":
        err = InvalidRequestError(message="The request body is not valid JSON.")
    elif path:
        err = InvalidRequestError(message=f"Invalid value for '{path}'.", param=path)
    else:
        err = InvalidRequestError()
    # Log field paths and error types only; never the submitted values.
    err.internal_detail = "; ".join(
        f"{_field_path(tuple(e.get('loc', ())))}: {e.get('type')}" for e in errors[:10]
    )
    log_error(err)
    return error_response(err, request)


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for this type only
    err_cls = _HTTP_STATUS_ERRORS.get(exc.status_code)
    if err_cls is None:
        err_cls = InvalidRequestError if exc.status_code < 500 else InternalError
    err = err_cls(f"http exception {exc.status_code}")
    log_error(err)
    return error_response(err, request)


async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception(
        "unhandled exception",
        exc_info=exc,
        extra={"event": "unhandled_exception", "exc_type": type(exc).__name__},
    )
    return error_response(InternalError(), request)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ModelMuxError, handle_modelmux_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)
