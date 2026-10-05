"""OpenAI Chat Completions request/response models and request validation.

Pydantic models do the structural parsing (unknown fields are accepted and
never echoed). ``validate_request`` then applies the parameter rules from the
plan (section 6.3) and raises specific ``ModelMuxError``s.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from modelmux.errors import (
    InvalidRequestError,
    PayloadTooLargeError,
    UnsupportedContentError,
    UnsupportedParameterError,
)

# Accepted but ignored: the CLIs cannot honour them.
IGNORED_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "user",
    "metadata",
    "logit_bias",
    "service_level",
    "service_tier",
    "reasoning_effort",
    "store",
)

SYSTEM_ROLES = frozenset({"system", "developer"})
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ToolCallFunction(_Loose):
    name: str
    arguments: str


class ToolCall(_Loose):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ChatMessage(_Loose):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class FunctionDefinition(_Loose):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None


class ToolDefinition(_Loose):
    type: str
    function: FunctionDefinition


class JsonSchemaFormat(_Loose):
    name: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    strict: bool | None = None


class ResponseFormat(_Loose):
    type: str
    json_schema: JsonSchemaFormat | None = None


class StreamOptions(_Loose):
    include_usage: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool | None = False
    stream_options: StreamOptions | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormat | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    # Legacy function calling: always rejected.
    functions: Any = None
    function_call: Any = None

    def ignored_params(self) -> list[str]:
        """Names of accepted-but-ignored parameters present in the request."""
        extra = self.model_extra or {}
        return [name for name in IGNORED_PARAMS if extra.get(name) is not None]


# ---------------------------------------------------------------- responses


class PromptTokensDetails(BaseModel):
    cached_tokens: int = 0


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: PromptTokensDetails = Field(default_factory=PromptTokensDetails)


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str
    logprobs: None = None


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    system_fingerprint: str
    choices: list[Choice]
    usage: Usage

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        for choice in data["choices"]:
            if choice["message"]["tool_calls"] is None:
                del choice["message"]["tool_calls"]
        return data


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


# ---------------------------------------------------------------- validation


def message_text(message: ChatMessage, index: int) -> str:
    """The text of a message. Non-text content parts are rejected."""
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part_index, part in enumerate(content):
        param = f"messages.{index}.content.{part_index}"
        if part.get("type") != "text":
            raise UnsupportedContentError(
                f"content part type {str(part.get('type'))[:32]!r}", param=param
            )
        text = part.get("text")
        if not isinstance(text, str):
            raise InvalidRequestError(message=f"Invalid value for '{param}.text'.", param=param)
        parts.append(text)
    return "\n".join(parts)


def _validate_message(message: ChatMessage, index: int) -> None:
    role = message.role
    param = f"messages.{index}"
    if role == "function":
        raise UnsupportedParameterError(
            message="The legacy 'function' role is not supported; use 'tool'.",
            param=f"{param}.role",
        )
    if role not in SYSTEM_ROLES | {"user", "assistant", "tool"}:
        raise InvalidRequestError(
            message=f"Invalid value for '{param}.role'.", param=f"{param}.role"
        )
    message_text(message, index)  # rejects non-text parts
    if role == "assistant":
        if message.content is None and not message.tool_calls:
            raise InvalidRequestError(
                message=f"'{param}' must have content or tool_calls.", param=f"{param}.content"
            )
    elif message.tool_calls:
        raise InvalidRequestError(
            message=f"'{param}.tool_calls' is only allowed on assistant messages.",
            param=f"{param}.tool_calls",
        )
    if role == "tool" and not message.tool_call_id:
        raise InvalidRequestError(
            message=f"'{param}.tool_call_id' is required for tool messages.",
            param=f"{param}.tool_call_id",
        )
    if role != "assistant" and message.content is None:
        raise InvalidRequestError(
            message=f"'{param}.content' is required.", param=f"{param}.content"
        )


def _validate_tools(req: ChatCompletionRequest, max_tools: int, max_schema_bytes: int) -> None:
    tools = req.tools or []
    if len(tools) > max_tools:
        raise InvalidRequestError(message=f"At most {max_tools} tools are allowed.", param="tools")
    names: set[str] = set()
    for index, tool in enumerate(tools):
        param = f"tools.{index}"
        if tool.type != "function":
            raise UnsupportedParameterError(
                message="Only 'function' tools are supported.", param=f"{param}.type"
            )
        name = tool.function.name
        if not TOOL_NAME_RE.fullmatch(name):
            raise InvalidRequestError(
                message=f"Invalid value for '{param}.function.name'.",
                param=f"{param}.function.name",
            )
        if name in names:
            raise InvalidRequestError(
                message=f"Duplicate tool name at '{param}.function.name'.",
                param=f"{param}.function.name",
            )
        names.add(name)
        size = len(json.dumps(tool.function.model_dump(by_alias=True)).encode())
        if size > max_schema_bytes:
            raise PayloadTooLargeError(
                message=f"'{param}' exceeds {max_schema_bytes} bytes.", param=param
            )


def stop_sequences(req: ChatCompletionRequest, max_stop: int) -> list[str]:
    stop = req.stop
    if stop is None:
        return []
    stops = [stop] if isinstance(stop, str) else list(stop)
    if len(stops) > max_stop:
        raise InvalidRequestError(message=f"At most {max_stop} stop sequences.", param="stop")
    if any(not s for s in stops):
        raise InvalidRequestError(message="Stop sequences must be non-empty.", param="stop")
    return stops


def validate_request(
    req: ChatCompletionRequest,
    *,
    max_messages: int,
    max_tools: int,
    max_tool_schema_bytes: int,
    max_stop_sequences: int,
) -> None:
    """Apply the section 6.3 parameter rules. Raises ``ModelMuxError``."""
    if req.functions is not None or req.function_call is not None:
        param = "functions" if req.functions is not None else "function_call"
        raise UnsupportedParameterError(
            message="Legacy function calling is not supported; use 'tools'.", param=param
        )
    if req.n not in (None, 1):
        raise InvalidRequestError(message="Only n=1 is supported.", param="n")
    if req.logprobs:
        raise UnsupportedParameterError(message="logprobs is not supported.", param="logprobs")
    if req.top_logprobs:
        raise UnsupportedParameterError(
            message="top_logprobs is not supported.", param="top_logprobs"
        )
    if len(req.messages) > max_messages:
        raise InvalidRequestError(
            message=f"At most {max_messages} messages are allowed.", param="messages"
        )
    for index, message in enumerate(req.messages):
        _validate_message(message, index)
    _validate_tools(req, max_tools, max_tool_schema_bytes)
    stop_sequences(req, max_stop_sequences)
    if req.response_format is not None and req.response_format.type not in {
        "text",
        "json_object",
        "json_schema",
    }:
        raise InvalidRequestError(
            message="Invalid value for 'response_format.type'.", param="response_format.type"
        )
