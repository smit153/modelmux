"""POST /v1/chat/completions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from modelmux.api.auth import require_api_key
from modelmux.api.middleware import get_request_id, new_request_id, require_json_content_type
from modelmux.api.schemas import ChatCompletionRequest, validate_request
from modelmux.config import Settings
from modelmux.core.pipeline import Pipeline
from modelmux.errors import InvalidRequestError, UnsupportedParameterError
from modelmux.observability.logging import model_var

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
    if req.stream:
        raise UnsupportedParameterError(message="Streaming is not supported yet.", param="stream")
    if req.tools:
        raise UnsupportedParameterError(message="Tools are not supported yet.", param="tools")
    if req.response_format is not None and req.response_format.type != "text":
        raise UnsupportedParameterError(
            message="Structured output is not supported yet.", param="response_format"
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
    try:
        result = await run_until_disconnect(request, pipeline.complete(req, request_id))
    except ClientDisconnectedError:
        log.info("client closed the connection", extra={"event": "client_closed"})
        return Response(status_code=CLIENT_CLOSED_STATUS)

    headers = {}
    if ignored := req.ignored_params():
        headers[IGNORED_PARAMS_HEADER] = ",".join(ignored)
    if not result.usage_available:
        headers[USAGE_HEADER] = "unavailable"
    return JSONResponse(result.completion.to_dict(), headers=headers)
