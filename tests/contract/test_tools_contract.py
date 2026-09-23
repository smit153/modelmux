"""Tools and structured output through the OpenAI SDK and LangChain, on both drivers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from tests.conftest import TEST_API_KEY
from tests.helpers import DEFAULT_MODEL

WEATHER_TOOL: Any = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class Person(BaseModel):
    name: str
    age: int


@pytest.fixture(params=["claude", "codex"])
def driver_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def model(driver_name: str) -> str:
    return DEFAULT_MODEL[driver_name]


@pytest.fixture
def sdk(client: TestClient) -> openai.OpenAI:
    return openai.OpenAI(
        base_url="http://testserver/v1", api_key=TEST_API_KEY, http_client=client, max_retries=0
    )


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., None]:
    monkeypatch.setenv("FAKE_COUNTER", str(tmp_path / "counter"))
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "last.json"))

    def script(*texts: str) -> None:
        monkeypatch.setenv("FAKE_REPLIES", json.dumps(list(texts)))

    return script


def tool_reply(*calls: tuple[str, dict[str, Any]]) -> str:
    return json.dumps({"tool_calls": [{"name": n, "arguments": a} for n, a in calls]})


def test_sdk_tools_round_trip(
    sdk: openai.OpenAI, model: str, replies: Callable[..., None], tmp_path: Path
) -> None:
    replies(
        tool_reply(("get_weather", {"city": "Oslo"}), ("get_weather", {"city": "Rome"})),
        "Oslo is 5C and Rome is 20C.",
    )
    messages: list[Any] = [{"role": "user", "content": "Weather in Oslo and Rome?"}]
    first = sdk.chat.completions.create(model=model, messages=messages, tools=[WEATHER_TOOL])
    choice = first.choices[0]
    assert choice.finish_reason == "tool_calls"
    tool_calls = choice.message.tool_calls or []
    assert [json.loads(c.function.arguments)["city"] for c in tool_calls] == ["Oslo", "Rome"]  # type: ignore[union-attr]

    # The client runs the tools and sends the results back.
    messages.append(choice.message.model_dump(exclude_none=True))
    for call, result in zip(tool_calls, ["5C", "20C"], strict=True):
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    second = sdk.chat.completions.create(model=model, messages=messages, tools=[WEATHER_TOOL])
    assert second.choices[0].message.content == "Oslo is 5C and Rome is 20C."
    assert second.choices[0].finish_reason == "stop"

    transcript = json.loads((tmp_path / "last.json").read_text())["stdin"]
    assert '{"tool_calls":[{"name":"get_weather","arguments":{"city":"Oslo"}}' in transcript
    assert f"tool tool_call_id={tool_calls[0].id}>>\n5C" in transcript


def test_sdk_forced_tool_choice(
    sdk: openai.OpenAI, model: str, replies: Callable[..., None]
) -> None:
    replies(tool_reply(("get_weather", {"city": "Oslo"})))
    completion = sdk.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=[WEATHER_TOOL],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
    )
    calls = completion.choices[0].message.tool_calls or []
    assert calls[0].function.name == "get_weather"  # type: ignore[union-attr]


def test_sdk_streaming_tool_calls(
    sdk: openai.OpenAI, model: str, replies: Callable[..., None]
) -> None:
    replies(tool_reply(("get_weather", {"city": "Oslo"})))
    stream = sdk.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "Hi"}], tools=[WEATHER_TOOL],
        stream=True,
    )  # fmt: skip
    names, arguments, finish = [], "", None
    for chunk in stream:
        delta = chunk.choices[0].delta
        for call in delta.tool_calls or []:
            if call.function and call.function.name:
                names.append(call.function.name)
            if call.function and call.function.arguments:
                arguments += call.function.arguments
        finish = chunk.choices[0].finish_reason or finish
    assert names == ["get_weather"]
    assert json.loads(arguments) == {"city": "Oslo"}
    assert finish == "tool_calls"


def test_sdk_json_schema_parse(
    sdk: openai.OpenAI, model: str, replies: Callable[..., None]
) -> None:
    replies('{"name": "Ada", "age": 36}')
    completion = sdk.chat.completions.parse(
        model=model, messages=[{"role": "user", "content": "Who?"}], response_format=Person
    )
    assert completion.choices[0].message.parsed == Person(name="Ada", age=36)


# ------------------------------------------------------------------ LangChain


@pytest.fixture
def chat_model(client: TestClient, model: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url="http://testserver/v1",
        api_key=TEST_API_KEY,  # type: ignore[arg-type]
        http_client=client,
        max_retries=0,
        # LangChain switches gpt-6* models with tools (and any "codex" model) to
        # the Responses API, which ModelMux does not implement.
        use_responses_api=False,
    )


@tool
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return f"sunny in {city}"


def test_langchain_bind_tools_round_trip(
    chat_model: ChatOpenAI, replies: Callable[..., None]
) -> None:
    replies(tool_reply(("get_weather", {"city": "Oslo"})), "It is sunny in Oslo.")
    llm = chat_model.bind_tools([get_weather])
    question = HumanMessage("Weather in Oslo?")
    ai = llm.invoke([question])
    assert isinstance(ai, AIMessage)
    assert ai.tool_calls[0]["name"] == "get_weather"
    assert ai.tool_calls[0]["args"] == {"city": "Oslo"}

    result = get_weather.invoke(ai.tool_calls[0]["args"])
    final = llm.invoke([question, ai, ToolMessage(result, tool_call_id=ai.tool_calls[0]["id"])])
    assert final.content == "It is sunny in Oslo."


def test_langchain_structured_output(chat_model: ChatOpenAI, replies: Callable[..., None]) -> None:
    replies('{"name": "Ada", "age": 36}')
    person = chat_model.with_structured_output(Person).invoke("Who?")
    assert person == Person(name="Ada", age=36)


def test_langchain_streaming(chat_model: ChatOpenAI, driver_name: str) -> None:
    text = "".join(str(chunk.content) for chunk in chat_model.stream("Hi"))
    assert (
        text
        == {"claude": "Hello from fake Claude.", "codex": "Hello from fake Codex."}[driver_name]
    )
