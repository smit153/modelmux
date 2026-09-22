"""Tool-call simulation (plan section 8.1-8.2).

The CLI never runs tools. Tool definitions go into the system prompt; the
model replies either with plain text or with exactly
``{"tool_calls": [{"name": ..., "arguments": {...}}]}``, which is parsed and
validated here without trusting it, then returned to the client to execute.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from modelmux.api.schemas import ChatCompletionRequest, ToolCall, ToolCallFunction
from modelmux.drivers.events import sanitize_detail
from modelmux.errors import InvalidRequestError

ToolMode = Literal["auto", "none", "required", "named"]
EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}
ERROR_LIMIT = 300


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    validator: Draft202012Validator


@dataclass(frozen=True)
class ToolPolicy:
    tools: dict[str, ToolSpec]
    mode: ToolMode
    forced: str | None
    parallel: bool


class ToolValidationError(Exception):
    """The model's tool reply is invalid. The message goes into the repair prompt."""


def _mode(req: ChatCompletionRequest, names: set[str]) -> tuple[ToolMode, str | None]:
    choice = req.tool_choice
    if choice is None or choice == "auto":
        return "auto", None
    if choice in ("none", "required"):
        return choice, None  # type: ignore[return-value]
    if isinstance(choice, dict) and choice.get("type") == "function":
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name in names:
            return "named", name
        raise InvalidRequestError(
            message="tool_choice names a function that is not in 'tools'.",
            param="tool_choice.function.name",
        )
    raise InvalidRequestError(message="Invalid value for 'tool_choice'.", param="tool_choice")


def build_tool_policy(req: ChatCompletionRequest) -> ToolPolicy | None:
    """Validate the request's tools (including their JSON schemas). None if no tools."""
    if not req.tools:
        if req.tool_choice not in (None, "auto", "none"):
            raise InvalidRequestError(message="tool_choice requires 'tools'.", param="tool_choice")
        return None
    specs: dict[str, ToolSpec] = {}
    for index, tool in enumerate(req.tools):
        parameters = tool.function.parameters or EMPTY_PARAMETERS
        try:
            Draft202012Validator.check_schema(parameters)
        except SchemaError:
            param = f"tools.{index}.function.parameters"
            raise InvalidRequestError(
                message=f"'{param}' is not a valid JSON Schema.", param=param
            ) from None
        specs[tool.function.name] = ToolSpec(
            name=tool.function.name,
            description=tool.function.description or "",
            parameters=parameters,
            validator=Draft202012Validator(parameters),
        )
    mode, forced = _mode(req, set(specs))
    return ToolPolicy(specs, mode, forced, parallel=req.parallel_tool_calls is not False)


def tool_instructions(policy: ToolPolicy) -> str:
    """The system-prompt section describing the tools and the reply protocol."""
    if policy.mode == "none":
        return "Do not call tools. Reply in plain text."
    listing = "\n".join(
        json.dumps(
            {"name": t.name, "description": t.description, "parameters": t.parameters},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for t in policy.tools.values()
    )
    lines = [
        "You can request tool calls. The application runs the tools and sends back the "
        "results in later turns; you never run anything yourself.",
        "",
        "Available tools (JSON, one per line):",
        listing,
        "",
        "To call tools, reply with ONLY a single JSON object and nothing else - no prose, "
        "no code fences:",
        '{"tool_calls": [{"name": "<tool name>", "arguments": {<arguments matching the '
        "tool's parameters schema>}}]}",
        "Otherwise, reply in plain text.",
    ]
    if policy.parallel:
        lines.append("You may include several calls in the list when they are independent.")
    else:
        lines.append("Include at most one call in the list.")
    if policy.mode == "required":
        lines.append("You MUST respond with a tool call.")
    elif policy.mode == "named":
        lines.append(f"You MUST call `{policy.forced}` and only `{policy.forced}`.")
    return "\n".join(lines)


def strip_fence(text: str) -> tuple[str, bool]:
    """Strip whitespace and one code fence wrapping the whole text."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
        inner = stripped[3:-3]
        first_newline = inner.find("\n")
        if first_newline >= 0 and inner[:first_newline].strip().isidentifier():
            inner = inner[first_newline + 1 :]  # language tag, e.g. ```json
        return inner.strip(), True
    return stripped, False


def first_json_object(text: str) -> Any:
    """Parse ``text``, or else the first balanced top-level ``{...}`` in it."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    except RecursionError:
        raise ValueError("JSON is nested too deeply") from None
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, start)
    except RecursionError:
        raise ValueError("JSON is nested too deeply") from None
    return obj


@dataclass(frozen=True)
class ParsedToolReply:
    kind: Literal["tool_calls", "text", "invalid"]
    calls: list[ToolCall]
    error: str = ""


def new_call_id() -> str:
    return "call_" + secrets.token_hex(12)


def _validate_calls(
    data: dict[str, Any], policy: ToolPolicy, max_args_bytes: int
) -> list[ToolCall]:
    if set(data) != {"tool_calls"}:
        raise ToolValidationError('The object must have exactly one key, "tool_calls".')
    raw_calls = data["tool_calls"]
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ToolValidationError('"tool_calls" must be a non-empty list.')
    if policy.mode == "none":
        raise ToolValidationError("Tools are disabled for this request; reply in plain text.")
    if not policy.parallel and len(raw_calls) > 1:
        raise ToolValidationError("Only one tool call is allowed.")
    calls = []
    for index, raw in enumerate(raw_calls):
        where = f"tool_calls[{index}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ToolValidationError(f'{where} must be an object with a string "name".')
        if not isinstance(raw.get("arguments"), dict):
            raise ToolValidationError(f"{where}.arguments must be a JSON object.")
        name, arguments = raw["name"], raw["arguments"]
        spec = policy.tools.get(name)
        if spec is None:
            raise ToolValidationError(f"{where}: unknown tool {sanitize_detail(name, 64)!r}.")
        if policy.mode == "named" and name != policy.forced:
            raise ToolValidationError(f"You must call `{policy.forced}` and only that tool.")
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > max_args_bytes:
            raise ToolValidationError(f"{where}.arguments is too large.")
        error = next(iter(spec.validator.iter_errors(arguments)), None)
        if error is not None:
            path = "/".join(str(p) for p in error.absolute_path) or "(root)"
            raise ToolValidationError(
                f"{where}.arguments does not match the schema of {name!r} at {path}: "
                f"{error.message}"
            )
        calls.append(
            ToolCall(id=new_call_id(), function=ToolCallFunction(name=name, arguments=encoded))
        )
    return calls


def parse_tool_reply(text: str, policy: ToolPolicy, max_args_bytes: int) -> ParsedToolReply:
    """Classify a reply as valid tool calls, plain text, or an invalid attempt."""
    body, fenced = strip_fence(text)
    looks_like_attempt = fenced or body.startswith("{")
    if not looks_like_attempt:
        return ParsedToolReply("text", [])
    try:
        data = first_json_object(body)
    except ValueError:
        return ParsedToolReply("invalid", [], "The reply is not valid JSON.")
    if not isinstance(data, dict) or "tool_calls" not in data:
        # JSON without tool_calls is a text answer (e.g. structured output).
        return ParsedToolReply("text", [])
    try:
        calls = _validate_calls(data, policy, max_args_bytes)
    except ToolValidationError as exc:
        return ParsedToolReply("invalid", [], sanitize_detail(str(exc), ERROR_LIMIT))
    return ParsedToolReply("tool_calls", calls)
