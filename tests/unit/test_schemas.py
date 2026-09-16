from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from modelmux import errors as e
from modelmux.api.schemas import (
    ChatCompletion,
    ChatCompletionRequest,
    Choice,
    ResponseMessage,
    ToolCall,
    ToolCallFunction,
    Usage,
    message_text,
    stop_sequences,
    validate_request,
)

LIMITS = {"max_messages": 5, "max_tools": 2, "max_tool_schema_bytes": 512, "max_stop_sequences": 2}


def req(**kwargs: Any) -> ChatCompletionRequest:
    body: dict[str, Any] = {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]}
    body.update(kwargs)
    return ChatCompletionRequest.model_validate(body)


def check(**kwargs: Any) -> None:
    validate_request(req(**kwargs), **LIMITS)


def expect(cls: type[e.ModelMuxError], param: str | None = None, **kwargs: Any) -> None:
    with pytest.raises(cls) as info:
        check(**kwargs)
    if param is not None:
        assert info.value.param == param


def test_minimal_ok() -> None:
    check()


def test_all_roles_ok() -> None:
    check(
        messages=[
            {"role": "system", "content": "s"},
            {"role": "developer", "content": [{"type": "text", "text": "d"}]},
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "r"},
        ]
    )


@pytest.mark.parametrize("body", [{"messages": []}, {"model": ""}, {"messages": "hi"}])
def test_structural_errors(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        req(**body)


def test_too_many_messages() -> None:
    expect(e.InvalidRequestError, "messages", messages=[{"role": "user", "content": "x"}] * 6)


def test_image_part_rejected() -> None:
    expect(
        e.UnsupportedContentError,
        "messages.0.content.1",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ],
    )


def test_text_part_without_text() -> None:
    expect(e.InvalidRequestError, messages=[{"role": "user", "content": [{"type": "text"}]}])


@pytest.mark.parametrize(
    ("message", "cls", "param"),
    [
        ({"role": "function", "name": "f", "content": "x"}, e.UnsupportedParameterError,
         "messages.0.role"),
        ({"role": "robot", "content": "x"}, e.InvalidRequestError, "messages.0.role"),
        ({"role": "assistant"}, e.InvalidRequestError, "messages.0.content"),
        ({"role": "tool", "content": "x"}, e.InvalidRequestError, "messages.0.tool_call_id"),
        ({"role": "user"}, e.InvalidRequestError, "messages.0.content"),
        ({"role": "user", "content": "x", "tool_calls": [
            {"id": "c", "function": {"name": "f", "arguments": "{}"}}]},
         e.InvalidRequestError, "messages.0.tool_calls"),
    ],
)  # fmt: skip
def test_bad_messages(message: dict[str, Any], cls: type[e.ModelMuxError], param: str) -> None:
    expect(cls, param, messages=[message])


@pytest.mark.parametrize(
    ("kwargs", "cls", "param"),
    [
        ({"functions": [{"name": "f"}]}, e.UnsupportedParameterError, "functions"),
        ({"function_call": "auto"}, e.UnsupportedParameterError, "function_call"),
        ({"n": 2}, e.InvalidRequestError, "n"),
        ({"logprobs": True}, e.UnsupportedParameterError, "logprobs"),
        ({"top_logprobs": 3}, e.UnsupportedParameterError, "top_logprobs"),
        ({"response_format": {"type": "xml"}}, e.InvalidRequestError, "response_format.type"),
        ({"stop": ["a", "b", "c"]}, e.InvalidRequestError, "stop"),
        ({"stop": [""]}, e.InvalidRequestError, "stop"),
    ],
)
def test_bad_params(kwargs: dict[str, Any], cls: type[e.ModelMuxError], param: str) -> None:
    expect(cls, param, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{"n": 1}, {"logprobs": False}, {"logprobs": None}, {"top_logprobs": 0},
     {"response_format": {"type": "json_object"}}, {"stop": "END"}],
)  # fmt: skip
def test_ok_params(kwargs: dict[str, Any]) -> None:
    check(**kwargs)


def fn_tool(name: str = "get_weather", **fn: Any) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}, **fn}}


def test_tools_ok() -> None:
    check(tools=[fn_tool("a"), fn_tool("b")])


@pytest.mark.parametrize(
    ("tools", "cls", "param"),
    [
        ([fn_tool("a"), fn_tool("b"), fn_tool("c")], e.InvalidRequestError, "tools"),
        ([{"type": "code_interpreter", "function": {"name": "x"}}], e.UnsupportedParameterError,
         "tools.0.type"),
        ([fn_tool("bad name")], e.InvalidRequestError, "tools.0.function.name"),
        ([fn_tool("a"), fn_tool("a")], e.InvalidRequestError, "tools.1.function.name"),
        ([fn_tool("a", description="x" * 600)], e.PayloadTooLargeError, "tools.0"),
    ],
)  # fmt: skip
def test_bad_tools(tools: list[dict[str, Any]], cls: type[e.ModelMuxError], param: str) -> None:
    expect(cls, param, tools=tools)


def test_ignored_params_listed_and_unknown_allowed() -> None:
    r = req(temperature=0.2, max_tokens=10, seed=None, some_future_field={"x": 1})
    assert r.ignored_params() == ["temperature", "max_tokens"]
    validate_request(r, **LIMITS)


def test_message_text_joins_parts() -> None:
    r = req(messages=[{"role": "user", "content": [{"type": "text", "text": "a"},
                                                    {"type": "text", "text": "b"}]}])  # fmt: skip
    assert message_text(r.messages[0], 0) == "a\nb"


def test_stop_sequences() -> None:
    assert stop_sequences(req(stop="X"), 4) == ["X"]
    assert stop_sequences(req(stop=["A", "B"]), 4) == ["A", "B"]
    assert stop_sequences(req(), 4) == []


def test_response_dump_omits_null_tool_calls() -> None:
    completion = ChatCompletion(
        id="chatcmpl-x",
        created=1,
        model="sonnet",
        system_fingerprint="fp",
        choices=[Choice(message=ResponseMessage(content="hi"), finish_reason="stop")],
        usage=Usage(),
    )
    data = completion.to_dict()
    assert "tool_calls" not in data["choices"][0]["message"]
    assert data["object"] == "chat.completion"

    call = ToolCall(id="call_1", function=ToolCallFunction(name="f", arguments="{}"))
    completion.choices[0] = Choice(
        message=ResponseMessage(content=None, tool_calls=[call]), finish_reason="tool_calls"
    )
    data = completion.to_dict()
    assert data["choices"][0]["message"]["content"] is None
    assert data["choices"][0]["message"]["tool_calls"][0]["type"] == "function"
