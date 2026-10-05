"""Live smoke test against the real Claude Code CLI. Skipped by default.

Run manually (makes two small sonnet requests: the startup login check and
one chat completion):

    LIVE_DRIVER_HOME=$HOME uv run pytest -m live tests/live
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import openai
import pytest
from fastapi.testclient import TestClient

from modelmux.config import load_settings
from modelmux.main import create_app
from modelmux.runtime.workspace import prepare_work_root
from tests.conftest import TEST_API_KEY
from tests.helpers import AUTH, chat_body

pytestmark = pytest.mark.live


@pytest.fixture
def live_client(tmp_path: Path) -> TestClient:
    binary = shutil.which("claude")
    if binary is None:
        pytest.skip("claude CLI not installed")
    home = os.environ.get("LIVE_DRIVER_HOME")
    if not home:
        pytest.skip("set LIVE_DRIVER_HOME to the home that holds the Claude login")
    settings = load_settings(
        driver="claude",
        api_keys=TEST_API_KEY,
        cli_path=Path(binary),
        driver_home=Path(home),
        work_root=tmp_path / "work",
        models={"sonnet": "sonnet"},  # live tests never use other models
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_live_chat(live_client: TestClient) -> None:
    with live_client as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        resp = client.post(
            "/v1/chat/completions",
            json=chat_body("Reply with exactly the word: pineapple"),
            headers=AUTH,
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "pineapple" in data["choices"][0]["message"]["content"].lower()
    assert data["usage"]["completion_tokens"] > 0
    assert data["usage"]["prompt_tokens"] > 0


def test_live_stream(tmp_path: Path) -> None:
    """Exactly one sonnet request: real partial messages arrive as several chunks.

    The startup probe (a second request) is skipped on purpose: the client is
    used without its lifespan, so only the streamed request reaches the API.
    """
    binary = shutil.which("claude")
    home = os.environ.get("LIVE_DRIVER_HOME")
    if binary is None or not home:
        pytest.skip("needs the claude CLI and LIVE_DRIVER_HOME")
    settings = load_settings(
        driver="claude",
        api_keys=TEST_API_KEY,
        cli_path=Path(binary),
        driver_home=Path(home),
        work_root=tmp_path / "work",
        models={"sonnet": "sonnet"},
    )
    prepare_work_root(settings.work_root)
    client = TestClient(create_app(settings))
    sdk = openai.OpenAI(
        base_url="http://testserver/v1", api_key=TEST_API_KEY, http_client=client, max_retries=0
    )
    stream = sdk.chat.completions.create(
        model="sonnet",
        messages=[{"role": "user", "content": "Count from 1 to 30 in words, comma separated."}],
        stream=True,
        stream_options={"include_usage": True},
    )
    pieces = []
    usage = None
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            pieces.append(chunk.choices[0].delta.content)
        usage = chunk.usage or usage
    text = "".join(pieces).lower()
    assert "thirty" in text
    assert len(pieces) > 1, "expected incremental deltas, got one chunk"
    assert usage is not None
    assert usage.completion_tokens > 0


def _sdk_without_probe(tmp_path: Path) -> openai.OpenAI:
    """An SDK client on a live app whose startup probe is skipped (saves a request)."""
    binary = shutil.which("claude")
    home = os.environ.get("LIVE_DRIVER_HOME")
    if binary is None or not home:
        pytest.skip("needs the claude CLI and LIVE_DRIVER_HOME")
    settings = load_settings(
        driver="claude",
        api_keys=TEST_API_KEY,
        cli_path=Path(binary),
        driver_home=Path(home),
        work_root=tmp_path / "work",
        models={"sonnet": "sonnet"},
    )
    prepare_work_root(settings.work_root)
    return openai.OpenAI(
        base_url="http://testserver/v1",
        api_key=TEST_API_KEY,
        http_client=TestClient(create_app(settings)),
        max_retries=0,
    )


def test_live_tool_call(tmp_path: Path) -> None:
    """One sonnet request: the real model follows the simulated tool protocol."""
    sdk = _sdk_without_probe(tmp_path)
    completion = sdk.chat.completions.create(
        model="sonnet",
        messages=[{"role": "user", "content": "What's the weather in Oslo and in Rome?"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Current weather for one city",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
    )
    choice = completion.choices[0]
    assert choice.finish_reason == "tool_calls", choice.message.content
    cities = {json.loads(c.function.arguments)["city"] for c in choice.message.tool_calls or []}  # type: ignore[union-attr]
    assert {"Oslo", "Rome"} <= cities


def test_live_json_schema(tmp_path: Path) -> None:
    """One sonnet request: the real model returns JSON matching a strict schema."""
    sdk = _sdk_without_probe(tmp_path)
    completion = sdk.chat.completions.create(
        model="sonnet",
        messages=[{"role": "user", "content": "Ada Lovelace: name and age at death."}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "person",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                    "required": ["name", "age"],
                },
            },
        },
    )
    data = json.loads(completion.choices[0].message.content or "")
    assert data["age"] == 36
    assert "Ada" in data["name"]
