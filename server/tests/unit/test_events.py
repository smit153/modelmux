from __future__ import annotations

import pytest

from modelmux import errors as e
from modelmux.drivers.events import (
    FailureKind,
    ProviderFailure,
    failure_to_error,
    looks_like_execution,
    sanitize_detail,
)


@pytest.mark.parametrize(
    "name",
    ["command_execution", "exec", "tool_use", "file_change", "apply_patch", "mcp_tool_call",
     "web_search", "shell", "ToolProgress", "server_tool_use"],
)  # fmt: skip
def test_execution_like(name: str) -> None:
    assert looks_like_execution(name)


@pytest.mark.parametrize(
    "name", ["text", "thinking", "assistant", "result", "rate_limit_event", "stream_event", None]
)
def test_not_execution_like(name: str | None) -> None:
    assert not looks_like_execution(name)


def test_sanitize_detail() -> None:
    assert sanitize_detail("a\x1b[31mb\nc") == "a?[31mb?c"
    assert sanitize_detail("é") == "?"
    long = sanitize_detail("x" * 500)
    assert len(long) == 200
    assert long.endswith("...")


@pytest.mark.parametrize(
    ("kind", "cls"),
    [
        (FailureKind.RATE_LIMITED, e.ProviderRateLimitedError),
        (FailureKind.AUTH, e.ProviderAuthError),
        (FailureKind.OVERLOADED, e.ProviderUnavailableError),
        (FailureKind.CONTEXT_LENGTH, e.ContextTooLargeError),
        (FailureKind.MODEL_UNAVAILABLE, e.ModelNotFoundError),
        (FailureKind.PROTOCOL, e.ProtocolError),
        (FailureKind.MAX_TURNS, e.ProviderError),
        (FailureKind.UNKNOWN, e.ProviderError),
    ],
)
def test_failure_mapping(kind: FailureKind, cls: type[e.ModelMuxError]) -> None:
    err = failure_to_error(ProviderFailure(kind, "SENTINEL provider text"))
    assert type(err) is cls
    assert "SENTINEL" not in str(err.to_openai())
    assert "SENTINEL" in str(err)


def test_retry_after_carried() -> None:
    err = failure_to_error(ProviderFailure(FailureKind.RATE_LIMITED, "x", retry_after=30))
    assert err.headers() == {"Retry-After": "30"}
