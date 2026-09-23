"""Turn raw model text into the final answer, or a reason it is invalid (plan 8.2-8.4)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from modelmux.api.schemas import ChatMessage, ToolCall
from modelmux.core.structured import FormatPolicy, FormatValidationError, validate_output
from modelmux.core.tools import ToolPolicy, parse_tool_reply

REPAIR_OUTPUT_LIMIT = 16 * 1024
TOOL_CALL_REQUIRED = (
    "A tool call is required. Reply with only the JSON object "
    '{"tool_calls": [{"name": ..., "arguments": {...}}]} and nothing else.'
)


@dataclass(frozen=True)
class Answer:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"


@dataclass(frozen=True)
class Invalid:
    """Why the output is unacceptable; safe to show the model in a repair turn."""

    error: str


def apply_stop(text: str, stops: Sequence[str]) -> tuple[str, bool]:
    """Truncate at the earliest stop sequence."""
    cut = min((i for s in stops if (i := text.find(s)) >= 0), default=-1)
    return (text[:cut], True) if cut >= 0 else (text, False)


def interpret(
    text: str,
    *,
    tools: ToolPolicy | None,
    fmt: FormatPolicy | None,
    stops: Sequence[str],
    max_args_bytes: int,
) -> Answer | Invalid:
    """Tool calls take priority; a text answer must satisfy ``fmt`` if set.

    Stop sequences apply only to free text, never to tool-call or format JSON.
    """
    if tools is not None:
        parsed = parse_tool_reply(text, tools, max_args_bytes)
        if parsed.kind == "tool_calls":
            return Answer(content=None, tool_calls=parsed.calls, finish_reason="tool_calls")
        if parsed.kind == "invalid":
            return Invalid(parsed.error)
        if tools.mode in ("required", "named"):
            return Invalid(TOOL_CALL_REQUIRED)
    if fmt is not None:
        try:
            return Answer(content=validate_output(text, fmt))
        except FormatValidationError as exc:
            return Invalid(str(exc))
    content, _stopped = apply_stop(text, stops)
    return Answer(content=content)


def repair_messages(
    messages: Sequence[ChatMessage], bad_output: str, error: str
) -> list[ChatMessage]:
    """The original conversation plus the invalid reply and a corrective turn."""
    return [
        *messages,
        ChatMessage(role="assistant", content=bad_output[:REPAIR_OUTPUT_LIMIT]),
        ChatMessage(
            role="user",
            content=(
                f"Your previous reply was invalid: {error}\n"
                "Reply again, following the required format exactly."
            ),
        ),
    ]
