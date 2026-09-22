from __future__ import annotations

import json
from typing import Any

import pytest

from modelmux.api.schemas import ChatCompletionRequest
from modelmux.core.tools import (
    ToolPolicy,
    build_tool_policy,
    first_json_object,
    parse_tool_reply,
    strip_fence,
    tool_instructions,
)
from modelmux.errors import InvalidRequestError

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "unit": {"enum": ["c", "f"]}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
TIME = {"type": "function", "function": {"name": "get_time"}}  # no parameters


def policy(**kwargs: Any) -> ToolPolicy:
    body: dict[str, Any] = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [WEATHER, TIME],
    }
    body.update(kwargs)
    result = build_tool_policy(ChatCompletionRequest.model_validate(body))
    assert result is not None
    return result


def reply(calls: list[dict[str, Any]]) -> str:
    return json.dumps({"tool_calls": calls})


# ------------------------------------------------------------------ policy


def test_no_tools() -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    req = ChatCompletionRequest.model_validate(body)
    assert build_tool_policy(req) is None


@pytest.mark.parametrize(
    ("choice", "mode", "forced"),
    [
        (None, "auto", None),
        ("auto", "auto", None),
        ("none", "none", None),
        ("required", "required", None),
        ({"type": "function", "function": {"name": "get_time"}}, "named", "get_time"),
    ],
)
def test_tool_choice(choice: Any, mode: str, forced: str | None) -> None:
    p = policy(tool_choice=choice)
    assert (p.mode, p.forced) == (mode, forced)


@pytest.mark.parametrize(
    ("kwargs", "param"),
    [
        ({"tool_choice": "sometimes"}, "tool_choice"),
        ({"tool_choice": {"type": "function", "function": {"name": "nope"}}},
         "tool_choice.function.name"),
        ({"tools": [{"type": "function", "function": {"name": "bad",
                                                      "parameters": {"type": "nonsense"}}}]},
         "tools.0.function.parameters"),
    ],
)  # fmt: skip
def test_invalid_policy(kwargs: dict[str, Any], param: str) -> None:
    with pytest.raises(InvalidRequestError) as info:
        policy(**kwargs)
    assert info.value.param == param


def test_tool_choice_without_tools() -> None:
    req = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "tool_choice": "required"}
    )
    with pytest.raises(InvalidRequestError):
        build_tool_policy(req)


def test_parallel_flag() -> None:
    assert policy().parallel
    assert not policy(parallel_tool_calls=False).parallel


# ------------------------------------------------------------------ instructions


def test_instructions_auto() -> None:
    text = tool_instructions(policy())
    assert '"name":"get_weather"' in text
    assert '"additionalProperties":false' in text
    assert "ONLY a single JSON object" in text
    assert "several calls" in text


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"tool_choice": "none"}, "Do not call tools."),
        ({"tool_choice": "required"}, "You MUST respond with a tool call."),
        ({"tool_choice": {"type": "function", "function": {"name": "get_time"}}},
         "You MUST call `get_time` and only `get_time`."),
        ({"parallel_tool_calls": False}, "at most one call"),
    ],
)  # fmt: skip
def test_instructions_variants(kwargs: dict[str, Any], expected: str) -> None:
    assert expected in tool_instructions(policy(**kwargs))


def test_none_mode_hides_tools() -> None:
    assert "get_weather" not in tool_instructions(policy(tool_choice="none"))


# ------------------------------------------------------------------ parsing helpers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  plain  ", ("plain", False)),
        ('```json\n{"a":1}\n```', ('{"a":1}', True)),
        ('```\n{"a":1}\n```', ('{"a":1}', True)),
        ("```x```", ("x", True)),
        ("```", ("```", False)),
    ],
)
def test_strip_fence(text: str, expected: tuple[str, bool]) -> None:
    assert strip_fence(text) == expected


def test_first_json_object() -> None:
    assert first_json_object('{"a": 1}') == {"a": 1}
    assert first_json_object('{"a": {"b": "}"}} trailing prose {"c": 2}') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):  # noqa: PT011
        first_json_object("no json here")
    with pytest.raises(ValueError):  # noqa: PT011
        first_json_object('{"unterminated": ')


# ------------------------------------------------------------------ parse_tool_reply

LIMIT = 1024


def test_valid_single_call() -> None:
    parsed = parse_tool_reply(reply([{"name": "get_weather", "arguments": {"city": "Oslo"}}]),
                              policy(), LIMIT)  # fmt: skip
    assert parsed.kind == "tool_calls"
    (call,) = parsed.calls
    assert call.id.startswith("call_")
    assert len(call.id) == 5 + 24
    assert call.type == "function"
    assert call.function.name == "get_weather"
    assert json.loads(call.function.arguments) == {"city": "Oslo"}


def test_two_calls_unique_ids() -> None:
    text = reply([{"name": "get_weather", "arguments": {"city": "A"}},
                  {"name": "get_time", "arguments": {}}])  # fmt: skip
    parsed = parse_tool_reply(text, policy(), LIMIT)
    assert [c.function.name for c in parsed.calls] == ["get_weather", "get_time"]
    assert parsed.calls[0].id != parsed.calls[1].id


def test_fenced_and_trailing_prose() -> None:
    fenced = "```json\n" + reply([{"name": "get_time", "arguments": {}}]) + "\n```"
    assert parse_tool_reply(fenced, policy(), LIMIT).kind == "tool_calls"
    trailing = reply([{"name": "get_time", "arguments": {}}]) + "\nLet me know!"
    assert parse_tool_reply(trailing, policy(), LIMIT).kind == "tool_calls"


@pytest.mark.parametrize(
    "text",
    ["The weather is nice.", 'Here is JSON: {"tool_calls": []}', '{"answer": 42}', "[1, 2]"],
)
def test_text_replies(text: str) -> None:
    assert parse_tool_reply(text, policy(), LIMIT).kind == "text"


@pytest.mark.parametrize(
    ("text", "kwargs", "error"),
    [
        ('{"tool_calls": [', {}, "not valid JSON"),
        ('{"tool_calls": []}', {}, "non-empty list"),
        ('{"tool_calls": "x"}', {}, "non-empty list"),
        ('{"tool_calls": [{"name": "get_time", "arguments": {}}], "extra": 1}', {},
         "exactly one key"),
        (reply([{"name": "rm_rf", "arguments": {}}]), {}, "unknown tool"),
        (reply([{"name": "get_weather", "arguments": {}}]), {}, "'city' is a required property"),
        (reply([{"name": "get_weather", "arguments": {"city": 5}}]), {}, "at city"),
        (reply([{"name": "get_weather", "arguments": {"city": "x", "evil": 1}}]), {},
         "Additional properties"),
        (reply([{"name": "get_weather", "arguments": "{\"city\": \"x\"}"}]), {}, "JSON object"),
        (reply([{"arguments": {}}]), {}, 'string "name"'),
        (reply([{"name": "get_time", "arguments": {}}] * 2), {"parallel_tool_calls": False},
         "Only one tool call"),
        (reply([{"name": "get_weather", "arguments": {"city": "x"}}]),
         {"tool_choice": {"type": "function", "function": {"name": "get_time"}}}, "must call"),
        (reply([{"name": "get_time", "arguments": {}}]), {"tool_choice": "none"}, "disabled"),
        (reply([{"name": "get_time", "arguments": {"pad": "x" * 2000}}]), {}, "too large"),
    ],
)  # fmt: skip
def test_invalid_attempts(text: str, kwargs: dict[str, Any], error: str) -> None:
    parsed = parse_tool_reply(text, policy(**kwargs), LIMIT)
    assert parsed.kind == "invalid"
    assert error in parsed.error
    assert not parsed.calls


def test_error_message_is_sanitized_and_bounded() -> None:
    evil = reply([{"name": "\x1b[31m" + "n" * 500, "arguments": {}}])
    parsed = parse_tool_reply(evil, policy(), LIMIT)
    assert "\x1b" not in parsed.error
    assert len(parsed.error) <= 300


def test_deeply_nested_json_does_not_crash() -> None:
    # Deep enough to hit Python's recursion limit in the JSON decoder.
    nested = '{"tool_calls":' + '{"a":' * 100_000 + "1" + "}" * 100_001
    parsed = parse_tool_reply(nested, policy(), LIMIT)
    assert parsed.kind == "invalid"
    assert "not valid JSON" in parsed.error
