from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux.drivers.claude.parser import classify_failure, parse_line
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
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude"


def parse_fixture(name: str) -> list[NormalizedEvent]:
    lines = (FIXTURES / name).read_bytes().splitlines()
    return [event for line in lines for event in parse_line(line)]


def of(events: list[NormalizedEvent], cls: type) -> list[NormalizedEvent]:
    return [e for e in events if isinstance(e, cls)]


def test_recorded_text_partial() -> None:
    events = parse_fixture("text_partial.jsonl")
    assert of(events, TextDelta) == [TextDelta("hello")]
    assert of(events, TextFinal) == [TextFinal("hello")]
    assert of(events, UsageReport) == [UsageReport(479, 4, 0, 0)]
    assert events[-1] == Completed(final_text="hello")
    assert not of(events, ToolAttempt)
    assert not of(events, ProviderFailure)


def test_recorded_tool_use_trips_on_first_signal() -> None:
    lines = (FIXTURES / "tool_use_read.jsonl").read_bytes().splitlines()
    tripped = [
        i
        for i, line in enumerate(lines)
        if any(isinstance(e, ToolAttempt) for e in parse_line(line))
    ]
    # 1st: the init event already lists an enabled tool.
    assert of(parse_line(lines[tripped[0]]), ToolAttempt) == [ToolAttempt("tools_enabled", "Read")]
    # 2nd: inside the model stream, content_block_start fires before the full
    # tool input and long before the tool_result.
    second = json.loads(lines[tripped[1]])
    assert second["event"]["type"] == "content_block_start"
    assert of(parse_line(lines[tripped[1]]), ToolAttempt) == [ToolAttempt("tool_use", "Read")]
    events = parse_fixture("tool_use_read.jsonl")
    kinds = [e.kind for e in of(events, ToolAttempt)]  # type: ignore[attr-defined]
    assert "tool_result" in kinds
    assert any(k.startswith("delta.input_json") for k in kinds)
    # Tool input (a file path) never leaks into the detail.
    assert all("note.txt" not in e.detail for e in of(events, ToolAttempt))  # type: ignore[attr-defined]


def test_recorded_model_not_found() -> None:
    events = parse_fixture("model_not_found.jsonl")
    failures = of(events, ProviderFailure)
    assert failures
    assert all(f.kind is FailureKind.MODEL_UNAVAILABLE for f in failures)  # type: ignore[attr-defined]
    # The synthetic error message is never treated as model output.
    assert not of(events, TextFinal)
    assert not of(events, Completed)


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("rate_limited.jsonl", FailureKind.RATE_LIMITED),
        ("auth_error.jsonl", FailureKind.AUTH),
        ("overloaded.jsonl", FailureKind.OVERLOADED),
        ("context_too_long.jsonl", FailureKind.CONTEXT_LENGTH),
        ("rate_limit_rejected.jsonl", FailureKind.RATE_LIMITED),
    ],
)
def test_provider_failures(fixture: str, kind: FailureKind) -> None:
    events = parse_fixture(fixture)
    failures = of(events, ProviderFailure)
    assert failures[0].kind is kind  # type: ignore[attr-defined]
    assert not of(events, TextFinal)
    assert not of(events, Completed)


def test_rate_limit_rejected_retry_after() -> None:
    failure = of(parse_fixture("rate_limit_rejected.jsonl"), ProviderFailure)[0]
    assert failure.retry_after == 3600.0  # type: ignore[attr-defined]  # capped


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("init_with_tools.jsonl", "tools_enabled"),
        ("init_with_mcp.jsonl", "tools_enabled"),
        ("hook_event.jsonl", "system.hook_started"),
        ("unknown_exec_event.jsonl", "event.tool_progress"),
        ("server_tool_use.jsonl", "server_tool_use"),
        ("permission_denied.jsonl", "permission_denied"),
        ("subagent_event.jsonl", "subagent"),
    ],
)
def test_tripwire_cases(fixture: str, kind: str) -> None:
    attempts = of(parse_fixture(fixture), ToolAttempt)
    assert [a.kind for a in attempts] == [kind]  # type: ignore[attr-defined]


def test_thinking_ignored() -> None:
    events = parse_fixture("thinking.jsonl")
    assert "".join(e.text for e in of(events, TextDelta)) == "Answer: 42"  # type: ignore[attr-defined]
    assert of(events, TextFinal) == [TextFinal("Answer: 42")]
    assert events[-1] == Completed("Answer: 42")
    assert not of(events, ToolAttempt)


def test_no_usage() -> None:
    events = parse_fixture("no_usage.jsonl")
    assert not of(events, UsageReport)
    assert events[-1] == Completed("hi")


def test_malformed_line() -> None:
    events = parse_fixture("malformed.jsonl")
    assert of(events, ProviderFailure) == [
        ProviderFailure(FailureKind.PROTOCOL, "unparseable output line")
    ]


@pytest.mark.parametrize(
    "line",
    [b"[]", b'"str"', b"{}", b'{"type": 5}', b"\xff\xfe", b'{"type":"assistant"}',
     b'{"type":"stream_event"}', b'{"type":"assistant","message":{"content":"x"}}',
     b'{"type":"assistant","message":{"content":[1]}}'],
)  # fmt: skip
def test_garbage_never_raises(line: bytes) -> None:
    events = parse_line(line)
    assert all(isinstance(e, ProviderFailure) for e in events)


def test_blank_line() -> None:
    assert parse_line(b"   ") == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"type":"keep_alive"}', Ignored("keep_alive")),
        (b'{"type":"command_output"}', ToolAttempt("event.command_output", "")),
        (b'{"type":"system","subtype":"compact_boundary"}', Ignored("system.compact_boundary")),
        (b'{"type":"system","subtype":"file_watch"}', ToolAttempt("system.file_watch", "")),
        (b'{"type":"user","message":{"role":"user","content":"hi"}}', Ignored("user")),
        (b'{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}',
         Ignored("rate_limit_event")),
        (b'{"type":"stream_event","event":{"type":"ping"}}', Ignored("stream.ping")),
        (b'{"type":"stream_event","event":{"type":"mcp_call"}}',
         ToolAttempt("stream.mcp_call", "")),
        (b'{"type":"stream_event","event":{"type":"content_block_start",'
         b'"content_block":{"type":"server_tool_use","name":"web_search"}}}',
         ToolAttempt("server_tool_use", "web_search")),
        (b'{"type":"stream_event","event":{"type":"content_block_delta",'
         b'"delta":{"type":"mystery_delta"}}}', ToolAttempt("delta.mystery_delta", "")),
    ],
)  # fmt: skip
def test_individual_events(line: bytes, expected: NormalizedEvent) -> None:
    assert parse_line(line) == [expected]


def test_recorded_commands_changed_is_benign() -> None:
    assert parse_fixture("commands_changed.jsonl") == [Ignored("system.commands_changed")]
    line = b'{"type":"system","subtype":"commands_changed","commands":[{"name":"x"}]}'
    assert parse_line(line) == [ToolAttempt("commands_enabled", "")]


def test_result_error_max_turns() -> None:
    line = b'{"type":"result","subtype":"error_max_turns","is_error":true}'
    assert parse_line(line) == [ProviderFailure(FailureKind.MAX_TURNS, "max turns reached")]


@pytest.mark.parametrize(
    ("code", "status", "message", "kind"),
    [
        (None, 429, "", FailureKind.RATE_LIMITED),
        (None, 401, "", FailureKind.AUTH),
        (None, 529, "", FailureKind.OVERLOADED),
        (None, 400, "prompt is too long: 250000 tokens", FailureKind.CONTEXT_LENGTH),
        ("rate_limit", 500, "", FailureKind.RATE_LIMITED),
        ("billing_error", None, "", FailureKind.AUTH),
        (None, None, "Claude usage limit reached", FailureKind.RATE_LIMITED),
        (None, None, "Not logged in · Please run /login", FailureKind.AUTH),
        (None, None, "something odd", FailureKind.UNKNOWN),
    ],
)
def test_classify(code: str | None, status: int | None, message: str, kind: FailureKind) -> None:
    assert classify_failure(code, status, message) is kind


def test_deeply_nested_line_never_raises() -> None:
    line = ('{"type":"assistant","x":' + "[" * 100_000 + "]" * 100_000 + "}").encode()
    assert parse_line(line) == [ProviderFailure(FailureKind.PROTOCOL, "unparseable output line")]
