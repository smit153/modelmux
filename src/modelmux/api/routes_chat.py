"""POST /v1/chat/completions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing

import anyio
from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from modelmux.api.auth import require_api_key
from modelmux.api.handlers import log_error
from modelmux.api.middleware import get_request_id, new_request_id, require_json_content_type
from modelmux.api.schemas import ChatCompletionRequest, validate_request
from modelmux.config import Settings
from modelmux.core.pipeline import Pipeline, StreamEnd, TextPiece, new_completion_id
from modelmux.core.streaming import DONE, ChunkBuilder, sse, sse_error
from modelmux.errors import (
    InternalError,
    InvalidRequestError,
    ModelMuxError,
    UnsupportedParameterError,
)
from modelmux.observability.logging import model_var
from modelmux.observability.metrics import Metrics

log = logging.getLogger("modelmux.api.chat")

router = APIRouter()

IGNORED_PARAMS_HEADER = "X-ModelMux-Ignored-Params"
USAGE_HEADER = "X-ModelMux-Usage"
CLIENT_CLOSED_STATUS = 499
DISCONNECT_POLL = 0.25


class ClientDisconnectedError(Exception):
    pass


async def run_until_disconnect[T](request: Request, work: Awaitable[T]) -> T:
    """Run ``work``; cancel it (killing the CLI) if the client goes away."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=DISCONNECT_POLL)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                await asyncio.wait({task})  # let the runner kill and reap the CLI
                raise ClientDisconnectedError
    finally:
        if not task.done():
            task.cancel()


async def parse_chat_request(request: Request) -> ChatCompletionRequest:
    body = await request.body()
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise InvalidRequestError(message="The request body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise InvalidRequestError(message="The request body must be a JSON object.")
    try:
        return ChatCompletionRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_input=False, include_url=False)) from None


def reject_unavailable_features(req: ChatCompletionRequest) -> None:
    """Features that land in later phases fail loudly rather than being ignored."""
    if req.tools:
        raise UnsupportedParameterError(message="Tools are not supported yet.", param="tools")
    if req.response_format is not None and req.response_format.type != "text":
        raise UnsupportedParameterError(
            message="Structured output is not supported yet.", param="response_format"
        )


class _Failure:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc


class StreamPump:
    """Runs a pipeline stream in its own asyncio task.

    Starlette's streaming response cancels its body through an anyio cancel
    scope, which cancels *every* await inside it. Keeping the CLI run in a
    separate task means a disconnect cancels it exactly once, so the runner
    can finish killing the process and removing the workspace.
    """

    def __init__(self, stream: AsyncGenerator[TextPiece | StreamEnd, None]) -> None:
        self._queue: asyncio.Queue[TextPiece | StreamEnd | _Failure] = asyncio.Queue(maxsize=1)
        self._task = asyncio.create_task(self._run(stream))

    async def _run(self, stream: AsyncGenerator[TextPiece | StreamEnd, None]) -> None:
        try:
            async with aclosing(stream):
                async for piece in stream:
                    await self._queue.put(piece)
        except Exception as exc:
            await self._queue.put(_Failure(exc))

    async def next(self) -> TextPiece | StreamEnd:
        item = await self._queue.get()
        if isinstance(item, _Failure):
            raise item.exc
        return item

    async def close(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.wait({self._task})


def _observe(request: Request, req: ChatCompletionRequest, outcome: str, started: float) -> None:
    metrics: Metrics | None = request.app.state.metrics
    if metrics is not None:
        pipeline: Pipeline = request.app.state.pipeline
        label = req.model if req.model in pipeline.models else "unknown"
        metrics.observe(label, outcome, time.monotonic() - started)


def _client_closed() -> Response:
    log.info("client closed the connection", extra={"event": "client_closed"})
    return Response(status_code=CLIENT_CLOSED_STATUS)


async def _sse_body(
    first: TextPiece | StreamEnd,
    pump: StreamPump,
    chunks: ChunkBuilder,
    finished: Callable[[str], None],
) -> AsyncIterator[bytes]:
    """The SSE stream. Errors after the first byte become one error event."""
    outcome = "ok"
    piece = first
    try:
        yield sse(chunks.role())
        while isinstance(piece, TextPiece):
            yield sse(chunks.content(piece.text))
            piece = await pump.next()
        yield sse(chunks.finish(piece.finish_reason))
        if chunks.include_usage:
            yield sse(chunks.usage(piece.usage))
        yield DONE
    except ModelMuxError as exc:
        outcome = exc.code
        log_error(exc)
        yield sse_error(exc)
        yield DONE
    except Exception:
        outcome = "internal_error"
        log.exception("stream failed", extra={"event": "unhandled_exception"})
        yield sse_error(InternalError())
        yield DONE
    except BaseException:
        outcome = "client_closed"
        log.info("client closed the stream", extra={"event": "client_closed"})
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await pump.close()
        finished(outcome)


async def _stream_response(
    request: Request, req: ChatCompletionRequest, request_id: str, headers: dict[str, str]
) -> Response:
    pipeline: Pipeline = request.app.state.pipeline
    started = time.monotonic()
    pump = StreamPump(pipeline.stream(req, request_id))
    # Wait for the first piece before sending headers, so that anything that
    # fails early (limits, timeouts, provider errors) is a normal HTTP error.
    try:
        first = await run_until_disconnect(request, pump.next())
    except ClientDisconnectedError:
        await pump.close()
        _observe(request, req, "client_closed", started)
        return _client_closed()
    except ModelMuxError as exc:
        await pump.close()
        _observe(request, req, exc.code, started)
        raise
    except BaseException:
        await pump.close()
        _observe(request, req, "internal_error", started)
        raise

    include_usage = bool(req.stream_options and req.stream_options.include_usage)
    chunks = ChunkBuilder(
        new_completion_id(), int(time.time()), req.model, pipeline.fingerprint, include_usage
    )

    def finished(outcome: str) -> None:
        _observe(request, req, outcome, started)

    return StreamingResponse(
        _sse_body(first, pump, chunks, finished),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
    )


@router.post(
    "/v1/chat/completions",
    dependencies=[Depends(require_api_key), Depends(require_json_content_type)],
)
async def chat_completions(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    pipeline: Pipeline = request.app.state.pipeline

    req = await parse_chat_request(request)
    model_var.set(req.model)
    validate_request(
        req,
        max_messages=settings.max_messages,
        max_tools=settings.max_tools,
        max_tool_schema_bytes=settings.max_tool_schema_bytes,
        max_stop_sequences=settings.max_stop_sequences,
    )
    reject_unavailable_features(req)

    request_id = get_request_id(request.scope) or new_request_id()
    headers = {}
    if ignored := req.ignored_params():
        headers[IGNORED_PARAMS_HEADER] = ",".join(ignored)
    if req.stream:
        return await _stream_response(request, req, request_id, headers)

    started = time.monotonic()
    outcome = "ok"
    try:
        result = await run_until_disconnect(request, pipeline.complete(req, request_id))
    except ClientDisconnectedError:
        outcome = "client_closed"
        return _client_closed()
    except ModelMuxError as exc:
        outcome = exc.code
        raise
    except Exception:
        outcome = "internal_error"
        raise
    finally:
        _observe(request, req, outcome, started)

    if not result.usage_available:
        headers[USAGE_HEADER] = "unavailable"
    return JSONResponse(result.completion.to_dict(), headers=headers)
