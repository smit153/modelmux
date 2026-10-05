"""Tools, structured output and repair through the API, on both drivers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import AUTH, assert_openai_error, chat_body

URL = "/v1/chat/completions"

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
TIME = {"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}
PERSON = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
}


def calls(*items: tuple[str, dict[str, Any]]) -> str:
    return json.dumps({"tool_calls": [{"name": n, "arguments": a} for n, a in items]})


@pytest.fixture(params=["claude", "codex"])
def driver_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def body(driver_name: str) -> Callable[..., dict[str, Any]]:
    def factory(**kwargs: Any) -> dict[str, Any]:
        return chat_body("What's the weather in Oslo?", driver=driver_name, **kwargs)

    return factory


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., Path]:
    """Script the model: one reply per CLI invocation. Returns the counter file."""
    counter = tmp_path / "counter"
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "last.json"))

    def script(*texts: str) -> Path:
        monkeypatch.setenv("FAKE_REPLIES", json.dumps(list(texts)))
        return counter

    return script


def invocations(counter: Path) -> int:
    return int(counter.read_text()) if counter.exists() else 0


def last_invocation(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "last.json").read_text()))


def message(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["choices"][0])


# ------------------------------------------------------------------ tool calls


def test_tool_call(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path],
    tmp_path: Path,
) -> None:  # fmt: skip
    counter = replies(calls(("get_weather", {"city": "Oslo"})))
    choice = message(client.post(URL, json=body(tools=[WEATHER, TIME]), headers=AUTH))
    assert choice["finish_reason"] == "tool_calls"
    msg = choice["message"]
    assert msg["content"] is None
    (call,) = msg["tool_calls"]
    assert call["id"].startswith("call_")
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Oslo"}
    assert invocations(counter) == 1
    system = last_invocation(tmp_path)["files"]["system-prompt.txt"]["text"]
    assert '"name":"get_weather"' in system
    assert "ONLY a single JSON object" in system


def test_two_calls_in_one_reply(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    replies(calls(("get_weather", {"city": "Oslo"}), ("get_time", {})))
    msg = message(client.post(URL, json=body(tools=[WEATHER, TIME]), headers=AUTH))["message"]
    names = [c["function"]["name"] for c in msg["tool_calls"]]
    assert names == ["get_weather", "get_time"]
    assert len({c["id"] for c in msg["tool_calls"]}) == 2


def test_text_answer_with_tools(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    replies("It is sunny. END of report")
    choice = message(client.post(URL, json=body(tools=[WEATHER], stop="END"), headers=AUTH))
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "It is sunny. "
    assert "tool_calls" not in choice["message"]


def test_stop_does_not_cut_tool_json(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    replies(calls(("get_weather", {"city": "Oslo"})))
    choice = message(client.post(URL, json=body(tools=[WEATHER], stop="Oslo"), headers=AUTH))
    args = json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])
    assert args == {"city": "Oslo"}


def test_tool_choice_none(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path],
    tmp_path: Path,
) -> None:  # fmt: skip
    replies("Plain answer.")
    choice = message(client.post(URL, json=body(tools=[WEATHER], tool_choice="none"), headers=AUTH))
    assert choice["message"]["content"] == "Plain answer."
    system = last_invocation(tmp_path)["files"]["system-prompt.txt"]["text"]
    assert "Do not call tools." in system
    assert "get_weather" not in system


# ------------------------------------------------------------------ repair


def test_repair_succeeds(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path],
    tmp_path: Path,
) -> None:  # fmt: skip
    counter = replies(calls(("get_weather", {})), calls(("get_weather", {"city": "Oslo"})))
    resp = client.post(URL, json=body(tools=[WEATHER]), headers=AUTH)
    choice = message(resp)
    assert choice["finish_reason"] == "tool_calls"
    assert invocations(counter) == 2
    assert resp.json()["usage"]["total_tokens"] == 40  # both runs counted
    repair_prompt = last_invocation(tmp_path)["stdin"]
    assert "Your previous reply was invalid" in repair_prompt
    assert "'city' is a required property" in repair_prompt


@pytest.mark.parametrize(
    ("kwargs", "first"),
    [
        ({"tool_choice": "required"}, "I think it is sunny."),
        ({"tool_choice": {"type": "function", "function": {"name": "get_weather"}}},
         calls(("get_time", {}))),
        ({"parallel_tool_calls": False}, calls(("get_time", {}), ("get_time", {}))),
        ({}, '{"tool_calls": [{"name": "get_weather", "arguments": '),
        ({}, calls(("delete_everything", {}))),
    ],
)  # fmt: skip
def test_invalid_first_reply_is_repaired(
    client: TestClient,
    body: Callable[..., dict[str, Any]],
    replies: Callable[..., Path],
    kwargs: dict[str, Any],
    first: str,
) -> None:
    counter = replies(first, calls(("get_weather", {"city": "Oslo"})))
    choice = message(client.post(URL, json=body(tools=[WEATHER, TIME], **kwargs), headers=AUTH))
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert invocations(counter) == 2


def test_exactly_one_repair_then_502(
    capfd: pytest.CaptureFixture[str],  # first, so the app's log handler is captured
    client: TestClient,
    body: Callable[..., dict[str, Any]],
    replies: Callable[..., Path],
) -> None:
    bad = calls(
        (
            "get_weather",
            {"city": "SECRET-OUTPUT"},
        )
    ).replace('"city"', '"town"')
    counter = replies(bad)
    resp = client.post(URL, json=body(tools=[WEATHER]), headers=AUTH)
    assert_openai_error(
        resp, 502, "invalid_model_output", "server_error", forbidden=("SECRET-OUTPUT", "town")
    )
    assert invocations(counter) == 2
    logs = capfd.readouterr().out
    assert '"event": "repair"' in logs
    assert "SECRET-OUTPUT" not in logs


# ------------------------------------------------------------------ structured output


def test_json_object(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    counter = replies("Sure! Here it is:", '```json\n{"ok": true}\n```')
    choice = message(
        client.post(URL, json=body(response_format={"type": "json_object"}), headers=AUTH)
    )
    assert json.loads(choice["message"]["content"]) == {"ok": True}
    assert invocations(counter) == 2


@pytest.mark.parametrize(
    ("reply", "strict", "status"),
    [
        ('{"name": "Ada", "age": 36}', True, 200),
        ('{"name": "Ada", "age": 36, "extra": 1}', False, 200),
        ('{"name": "Ada", "age": 36, "extra": 1}', True, 502),
        ('{"name": "Ada"}', False, 502),
    ],
)
def test_json_schema(
    client: TestClient,
    body: Callable[..., dict[str, Any]],
    replies: Callable[..., Path],
    reply: str,
    strict: bool,
    status: int,
) -> None:
    replies(reply)
    spec = {"name": "person", "schema": PERSON, "strict": strict}
    resp = client.post(
        URL, json=body(response_format={"type": "json_schema", "json_schema": spec}),
        headers=AUTH,
    )  # fmt: skip
    assert resp.status_code == status
    if status == 200:
        assert json.loads(resp.json()["choices"][0]["message"]["content"])["name"] == "Ada"


def test_tools_and_format_text_must_match_format(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    counter = replies("just prose", '{"answer": "sunny"}')
    request = body(tools=[WEATHER], response_format={"type": "json_object"})
    choice = message(client.post(URL, json=request, headers=AUTH))
    assert json.loads(choice["message"]["content"]) == {"answer": "sunny"}
    assert invocations(counter) == 2


# ------------------------------------------------------------------ request validation


def test_invalid_tool_schema_rejected_before_spawn(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    counter = replies("x")
    bad = {"type": "function", "function": {"name": "f", "parameters": {"type": 12}}}
    resp = client.post(URL, json=body(tools=[bad]), headers=AUTH)
    err = assert_openai_error(resp, 400, "invalid_request", "invalid_request_error")
    assert err["param"] == "tools.0.function.parameters"
    assert invocations(counter) == 0


# ------------------------------------------------------------------ buffered streaming


def sse_events(text: str) -> list[Any]:
    return [
        b.removeprefix("data: ") if b == "data: [DONE]" else json.loads(b.removeprefix("data: "))
        for b in text.split("\n\n")
        if b.startswith("data: ")
    ]


def test_stream_tool_calls(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    replies(calls(("get_weather", {"city": "Oslo"}), ("get_time", {})))
    request = body(tools=[WEATHER, TIME], stream=True, stream_options={"include_usage": True})
    events = sse_events(client.post(URL, json=request, headers=AUTH).text)
    assert events[-1] == "[DONE]"
    _role, tool_chunk, finish, usage = events[:-1]
    deltas = tool_chunk["choices"][0]["delta"]["tool_calls"]
    assert [d["index"] for d in deltas] == [0, 1]
    assert deltas[0]["function"]["name"] == "get_weather"
    assert deltas[0]["id"].startswith("call_")
    assert finish["choices"][0]["finish_reason"] == "tool_calls"
    assert usage["usage"]["total_tokens"] == 20


def test_stream_structured_output(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    replies('{"name": "Ada", "age": 36}')
    spec = {"name": "person", "schema": PERSON}
    request = body(stream=True, response_format={"type": "json_schema", "json_schema": spec})
    events = sse_events(client.post(URL, json=request, headers=AUTH).text)
    content = "".join(e["choices"][0]["delta"].get("content", "") for e in events[:-1])
    assert json.loads(content) == {"name": "Ada", "age": 36}


def test_stream_invalid_after_repair_is_http_error(
    client: TestClient, body: Callable[..., dict[str, Any]], replies: Callable[..., Path]
) -> None:
    replies("not a tool call")
    request = body(tools=[WEATHER], tool_choice="required", stream=True)
    resp = client.post(URL, json=request, headers=AUTH)
    assert_openai_error(resp, 502, "invalid_model_output", "server_error")
