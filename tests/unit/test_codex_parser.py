from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux.drivers.codex.parser import classify_failure, parse_line
from modelmux.drivers.events import (
    Completed,
    FailureKind,
    Ignored,
    NormalizedEvent,
    ProviderFailure,
    TextFinal,
    ToolAttempt,
    UsageReport,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def parse_fixture(name: str) -> list[NormalizedEvent]:
    lines = (FIXTURES / name).read_bytes().splitlines()
    return [event for line in lines for event in parse_line(line)]


def of(events: list[NormalizedEvent], cls: type) -> list[NormalizedEvent]:
    return [e for e in events if isinstance(e, cls)]


def test_recorded_auth_error() -> None:
    events = parse_fixture("auth_error.jsonl")
    failures = of(events, ProviderFailure)
    # Retry notices and the transport-fallback error item are not failures;
    # the final error event and turn.failed are.
    assert [f.kind for f in failures] == [FailureKind.AUTH, FailureKind.AUTH]  # type: ignore[attr-defined]
    assert Ignored("error.retry") in events
    assert Ignored("item.completed.error") in events
    assert not of(events, ToolAttempt)
    assert not of(events, Completed)


def test_text() -> None:
    events = parse_fixture("text.jsonl")
    assert of(events, TextFinal) == [TextFinal("Hello from Codex.")]
    # input_tokens (24) includes the 8 cached tokens.
    assert of(events, UsageReport) == [UsageReport(16, 5, 8, 0)]
    assert events[-1] == Completed(None)


def test_cache_write_tokens_subtracted() -> None:
    line = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 30,
                                             "cache_write_input_tokens": 20, "output_tokens": 1}}
    ).encode()  # fmt: skip
    assert parse_line(line)[0] == UsageReport(50, 1, 30, 20)


def test_reasoning_todo_and_updates_ignored() -> None:
    events = parse_fixture("reasoning_todo.jsonl")
    assert of(events, TextFinal) == [TextFinal("Hello")]
    assert not of(events, ToolAttempt)


def test_no_usage() -> None:
    events = parse_fixture("no_usage.jsonl")
    assert not of(events, UsageReport)
    assert events[-1] == Completed(None)


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("command_execution.jsonl", "command_execution"),
        ("file_change.jsonl", "file_change"),
        ("mcp_tool_call.jsonl", "mcp_tool_call"),
        ("web_search.jsonl", "web_search"),
        ("collab_tool_call.jsonl", "collab_tool_call"),
        ("unknown_exec_item.jsonl", "exec_patch_v2"),
        ("unknown_exec_event.jsonl", "event.shell.started"),
    ],
)
def test_tripwire(fixture: str, kind: str) -> None:
    lines = (FIXTURES / fixture).read_bytes().splitlines()
    first = next(
        i
        for i, line in enumerate(lines)
        if any(isinstance(e, ToolAttempt) for e in parse_line(line))
    )
    attempt = of(parse_line(lines[first]), ToolAttempt)[0]
    assert attempt.kind == kind  # type: ignore[attr-defined]
    event = json.loads(lines[first])
    if event["type"].startswith("item."):
        # Fires on item.started, i.e. before the action completes.
        assert event["type"] == "item.started"


def test_command_detail_never_includes_command() -> None:
    attempts = of(parse_fixture("command_execution.jsonl"), ToolAttempt)
    assert all("ls" not in a.detail for a in attempts)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("rate_limited.jsonl", FailureKind.RATE_LIMITED),
        ("usage_limit.jsonl", FailureKind.RATE_LIMITED),
        ("context_too_long.jsonl", FailureKind.CONTEXT_LENGTH),
        ("overloaded.jsonl", FailureKind.OVERLOADED),
        ("model_not_found.jsonl", FailureKind.MODEL_UNAVAILABLE),
    ],
)
def test_failures(fixture: str, kind: FailureKind) -> None:
    failures = of(parse_fixture(fixture), ProviderFailure)
    assert failures[-1].kind is kind  # type: ignore[attr-defined]


def test_malformed() -> None:
    assert of(parse_fixture("malformed.jsonl"), ProviderFailure) == [
        ProviderFailure(FailureKind.PROTOCOL, "unparseable output line")
    ]


@pytest.mark.parametrize(
    "line",
    [b"[]", b"{}", b'{"type":1}', b'{"type":"item.started"}', b'{"type":"item.started","item":5}',
     b"\xff"],
)  # fmt: skip
def test_garbage_never_raises(line: bytes) -> None:
    assert all(isinstance(e, ProviderFailure) for e in parse_line(line))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"", []),
        (b'{"type":"thread.started"}', [Ignored("thread.started")]),
        (b'{"type":"session.configured"}', [Ignored("session.configured")]),
        (b'{"type":"turn.failed"}', [ProviderFailure(FailureKind.UNKNOWN, "turn failed")]),
        (b'{"type":"item.started","item":{"type":"agent_message","text":"x"}}',
         [Ignored("item.started.agent_message")]),
        (b'{"type":"item.completed","item":{"type":"image_view"}}',
         [Ignored("item.completed.image_view")]),
    ],
)  # fmt: skip
def test_individual_events(line: bytes, expected: list[NormalizedEvent]) -> None:
    assert parse_line(line) == expected


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("unexpected status 401 Unauthorized", FailureKind.AUTH),
        ("Not logged in. Run codex login", FailureKind.AUTH),
        ("429 Too Many Requests", FailureKind.RATE_LIMITED),
        ("context_length_exceeded", FailureKind.CONTEXT_LENGTH),
        ("503 Service Unavailable", FailureKind.OVERLOADED),
        ("The model `x` does not exist", FailureKind.MODEL_UNAVAILABLE),
        ("something odd", FailureKind.UNKNOWN),
    ],
)
def test_classify(message: str, kind: FailureKind) -> None:
    assert classify_failure(message) is kind


def test_deeply_nested_line_never_raises() -> None:
    line = ('{"type":"item.started","x":' + "[" * 100_000 + "]" * 100_000 + "}").encode()
    assert parse_line(line) == [ProviderFailure(FailureKind.PROTOCOL, "unparseable output line")]
