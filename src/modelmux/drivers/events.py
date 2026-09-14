"""Normalized events: the only thing core code knows about CLI output.

Drivers turn every CLI stdout line into zero or more of these. Core never
parses CLI-specific formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from modelmux.errors import (
    ContextTooLargeError,
    ModelMuxError,
    ModelNotFoundError,
    ProtocolError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)

# Substrings that mark an event or item type as "something executed".
# Unknown types containing any of these fail closed as ToolAttempt.
EXECUTION_HINTS = ("command", "exec", "tool", "file", "patch", "mcp", "search", "shell")

DETAIL_LIMIT = 200
_UNSAFE_CHARS = re.compile(r"[^\x20-\x7e]")


class FailureKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    OVERLOADED = "overloaded"
    CONTEXT_LENGTH = "context_length"
    MAX_TURNS = "max_turns"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TextDelta:
    """Incremental assistant text (streaming)."""

    text: str


@dataclass(frozen=True)
class TextFinal:
    """Complete assistant text for (a block of) the turn."""

    text: str


@dataclass(frozen=True)
class ToolAttempt:
    """The CLI tried to use a tool / run a command / touch files. Always a violation."""

    kind: str
    detail: str


@dataclass(frozen=True)
class UsageReport:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Completed:
    """The CLI signalled a successful end of turn."""

    final_text: str | None = None


@dataclass(frozen=True)
class ProviderFailure:
    kind: FailureKind
    message: str
    retry_after: float | None = None


@dataclass(frozen=True)
class Ignored:
    event_type: str


NormalizedEvent = (
    TextDelta | TextFinal | ToolAttempt | UsageReport | Completed | ProviderFailure | Ignored
)


def looks_like_execution(*names: str | None) -> bool:
    """True if any of ``names`` suggests execution (fail-closed check)."""
    return any(name and any(hint in name.lower() for hint in EXECUTION_HINTS) for name in names)


def sanitize_detail(value: object, limit: int = DETAIL_LIMIT) -> str:
    """Printable ASCII only, truncated. For log/detail fields from CLI output."""
    text = _UNSAFE_CHARS.sub("?", str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


_FAILURE_ERRORS: dict[FailureKind, type[ModelMuxError]] = {
    FailureKind.RATE_LIMITED: ProviderRateLimitedError,
    FailureKind.AUTH: ProviderAuthError,
    FailureKind.OVERLOADED: ProviderUnavailableError,
    FailureKind.CONTEXT_LENGTH: ContextTooLargeError,
    FailureKind.MODEL_UNAVAILABLE: ModelNotFoundError,
    FailureKind.PROTOCOL: ProtocolError,
    FailureKind.MAX_TURNS: ProviderError,
    FailureKind.UNKNOWN: ProviderError,
}


def failure_to_error(failure: ProviderFailure) -> ModelMuxError:
    """Map a classified provider failure to the error returned to clients."""
    cls = _FAILURE_ERRORS[failure.kind]
    return cls(f"{failure.kind}: {failure.message}", retry_after=failure.retry_after)
