"""Parse Claude Code ``--output-format stream-json`` lines into normalized events.

Verified against Claude Code 2.1.285 (see tests/fixtures/claude). Anything
that could mean a tool ran, or was about to run, is a ``ToolAttempt``; the
earliest signal is a ``content_block_start`` of type ``tool_use`` in the
partial-message stream, which arrives before the tool input is complete.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from modelmux.drivers.events import (
    Completed,
    FailureKind,
    Ignored,
    NormalizedEvent,
    ProviderFailure,
    TextDelta,
    TextFinal,
    ToolAttempt,
    UsageReport,
    looks_like_execution,
    sanitize_detail,
)

TEXT_BLOCKS = frozenset({"text"})
REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking"})
MAX_RETRY_AFTER = 3600.0

_ERROR_CODES: dict[str, FailureKind] = {
    "rate_limit": FailureKind.RATE_LIMITED,
    "authentication_failed": FailureKind.AUTH,
    "oauth_org_not_allowed": FailureKind.AUTH,
    "billing_error": FailureKind.AUTH,
    "permission_error": FailureKind.AUTH,
    "model_not_found": FailureKind.MODEL_UNAVAILABLE,
    "server_error": FailureKind.OVERLOADED,
    "overloaded": FailureKind.OVERLOADED,
    "overloaded_error": FailureKind.OVERLOADED,
    "api_error": FailureKind.OVERLOADED,
}

_STATUS_CODES: dict[int, FailureKind] = {
    401: FailureKind.AUTH,
    403: FailureKind.AUTH,
    404: FailureKind.MODEL_UNAVAILABLE,
    413: FailureKind.CONTEXT_LENGTH,
    429: FailureKind.RATE_LIMITED,
    500: FailureKind.OVERLOADED,
    502: FailureKind.OVERLOADED,
    503: FailureKind.OVERLOADED,
    504: FailureKind.OVERLOADED,
    529: FailureKind.OVERLOADED,
}

_MESSAGE_HINTS: tuple[tuple[tuple[str, ...], FailureKind], ...] = (
    (("prompt is too long", "context length", "context window", "too many tokens"),
     FailureKind.CONTEXT_LENGTH),
    (("rate limit", "usage limit", "limit reached", "quota"), FailureKind.RATE_LIMITED),
    (("overloaded",), FailureKind.OVERLOADED),
    (("invalid api key", "/login", "not logged in", "authentication", "unauthorized",
      "credentials"), FailureKind.AUTH),
)  # fmt: skip


def classify_failure(code: str | None, status: int | None, message: str) -> FailureKind:
    lowered = message.lower()
    context_hints = _MESSAGE_HINTS[0][0]
    if any(hint in lowered for hint in context_hints):
        return FailureKind.CONTEXT_LENGTH
    if code and code in _ERROR_CODES:
        return _ERROR_CODES[code]
    if status is not None and status in _STATUS_CODES:
        return _STATUS_CODES[status]
    for hints, kind in _MESSAGE_HINTS:
        if any(hint in lowered for hint in hints):
            return kind
    return FailureKind.UNKNOWN


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return _str(content) or ""


def _block_events(block: Any) -> list[NormalizedEvent]:
    if not isinstance(block, dict):
        return [ProviderFailure(FailureKind.PROTOCOL, "content block is not an object")]
    kind = _str(block.get("type")) or "unknown"
    if kind in TEXT_BLOCKS:
        return [TextFinal(_str(block.get("text")) or "")]
    if kind in REASONING_BLOCKS:
        return [Ignored(f"block.{kind}")]
    # tool_use, server_tool_use, mcp_tool_use, *_tool_result and anything unknown:
    # a content block that is not text or reasoning is never expected here.
    name = _str(block.get("name")) or ""
    return [ToolAttempt(kind=sanitize_detail(kind, 64), detail=sanitize_detail(name))]


def _system(data: dict[str, Any]) -> list[NormalizedEvent]:
    subtype = _str(data.get("subtype")) or "unknown"
    if subtype == "init":
        tools = data.get("tools") or []
        servers = data.get("mcp_servers") or []
        if tools or servers:
            names = [str(t) for t in tools] + [str(s) for s in servers]
            return [ToolAttempt(kind="tools_enabled", detail=sanitize_detail(",".join(names)))]
        return [Ignored("system.init")]
    if subtype == "commands_changed":
        # Seen with Claude Code 2.1.285 under a minimal environment. Benign
        # only while no commands are available (slash commands are disabled).
        if data.get("commands"):
            return [ToolAttempt(kind="commands_enabled", detail="")]
        return [Ignored("system.commands_changed")]
    if "hook" in subtype or looks_like_execution(subtype):
        return [ToolAttempt(kind=f"system.{sanitize_detail(subtype, 48)}", detail="")]
    return [Ignored(f"system.{subtype}")]


def _stream_event(data: dict[str, Any]) -> list[NormalizedEvent]:
    event = data.get("event")
    if not isinstance(event, dict):
        return [ProviderFailure(FailureKind.PROTOCOL, "stream_event without event")]
    etype = _str(event.get("type")) or "unknown"
    if etype == "content_block_start":
        block = event.get("content_block")
        kind = _str(block.get("type")) if isinstance(block, dict) else None
        if kind in TEXT_BLOCKS or kind in REASONING_BLOCKS:
            return [Ignored(f"stream.{etype}")]
        return _block_events(block)
    if etype == "content_block_delta":
        delta = event.get("delta")
        dtype = _str(delta.get("type")) if isinstance(delta, dict) else None
        if dtype == "text_delta" and isinstance(delta, dict):
            return [TextDelta(_str(delta.get("text")) or "")]
        if dtype in {"thinking_delta", "signature_delta", "citations_delta"}:
            return [Ignored(f"stream.{dtype}")]
        # input_json_delta (tool input) or anything unknown fails closed.
        return [ToolAttempt(kind=f"delta.{sanitize_detail(dtype or 'unknown', 48)}", detail="")]
    if looks_like_execution(etype):
        return [ToolAttempt(kind=f"stream.{sanitize_detail(etype, 48)}", detail="")]
    return [Ignored(f"stream.{etype}")]


def _assistant(data: dict[str, Any]) -> list[NormalizedEvent]:
    message = data.get("message")
    if not isinstance(message, dict):
        return [ProviderFailure(FailureKind.PROTOCOL, "assistant event without message")]
    error = _str(data.get("error"))
    if error or data.get("is_api_error_message"):
        # The CLI reports API errors as a synthetic assistant message; its text
        # is an error description, never model output.
        text = _text_of(message.get("content"))
        kind = classify_failure(error, None, text)
        return [ProviderFailure(kind, sanitize_detail(f"{error}: {text}"))]
    content = message.get("content")
    if not isinstance(content, list):
        return [ProviderFailure(FailureKind.PROTOCOL, "assistant content is not a list")]
    events: list[NormalizedEvent] = []
    for block in content:
        events.extend(_block_events(block))
    return events


def _user(data: dict[str, Any]) -> list[NormalizedEvent]:
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if "tool_use_result" in data or (
        isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    ):
        return [ToolAttempt(kind="tool_result", detail="")]
    return [Ignored("user")]


def _usage(data: dict[str, Any]) -> UsageReport:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return UsageReport()
    return UsageReport(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cached_input_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
    )


def _result(data: dict[str, Any]) -> list[NormalizedEvent]:
    if data.get("permission_denials"):
        return [ToolAttempt(kind="permission_denied", detail="")]
    usage = data.get("usage")
    server_tools = usage.get("server_tool_use") if isinstance(usage, dict) else None
    if isinstance(server_tools, dict) and any(_int(v) > 0 for v in server_tools.values()):
        return [ToolAttempt(kind="server_tool_use", detail="")]

    subtype = _str(data.get("subtype")) or ""
    if data.get("is_error") or subtype != "success":
        if subtype == "error_max_turns":
            return [ProviderFailure(FailureKind.MAX_TURNS, "max turns reached")]
        status = data.get("api_error_status")
        message = _str(data.get("result")) or " ".join(str(e) for e in data.get("errors") or [])
        kind = classify_failure(None, status if isinstance(status, int) else None, message)
        return [ProviderFailure(kind, sanitize_detail(f"{subtype} {status}: {message}"))]

    events: list[NormalizedEvent] = [_usage(data)] if "usage" in data else []
    events.append(Completed(final_text=_str(data.get("result"))))
    return events


def _rate_limit(data: dict[str, Any]) -> list[NormalizedEvent]:
    info = data.get("rate_limit_info")
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return [Ignored("rate_limit_event")]
    retry_after = None
    resets_at = info.get("resetsAt")
    if isinstance(resets_at, int | float):
        retry_after = min(max(1.0, float(resets_at) - time.time()), MAX_RETRY_AFTER)
    kind = sanitize_detail(info.get("rateLimitType") or "unknown", 48)
    return [ProviderFailure(FailureKind.RATE_LIMITED, f"rejected: {kind}", retry_after)]


_HANDLERS: dict[str, Callable[[dict[str, Any]], list[NormalizedEvent]]] = {
    "system": _system,
    "stream_event": _stream_event,
    "assistant": _assistant,
    "user": _user,
    "result": _result,
    "rate_limit_event": _rate_limit,
}


def parse_line(line: bytes) -> list[NormalizedEvent]:
    """Translate one stream-json line. Never raises."""
    if not line.strip():
        return []
    try:
        data = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return [ProviderFailure(FailureKind.PROTOCOL, "unparseable output line")]
    if not isinstance(data, dict):
        return [ProviderFailure(FailureKind.PROTOCOL, "output line is not an object")]
    event_type = _str(data.get("type"))
    if event_type is None:
        return [ProviderFailure(FailureKind.PROTOCOL, "output line without a type")]
    if data.get("parent_tool_use_id"):
        return [ToolAttempt(kind="subagent", detail=sanitize_detail(event_type))]
    handler = _HANDLERS.get(event_type)
    try:
        if handler is not None:
            return handler(data)
    except Exception:  # defensive: a parser bug must not crash the request
        return [ProviderFailure(FailureKind.PROTOCOL, f"could not parse {event_type} event")]
    if looks_like_execution(event_type):
        return [ToolAttempt(kind=f"event.{sanitize_detail(event_type, 48)}", detail="")]
    return [Ignored(sanitize_detail(event_type, 64))]
