"""The official OpenAI Python SDK against the app (fake CLI), for both drivers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import openai
import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_API_KEY
from tests.helpers import DEFAULT_MODEL, FAKE_REPLY_TEXT, use_fixture


@pytest.fixture(params=["claude", "codex"])
def driver_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def sdk_factory(client: TestClient) -> Callable[..., openai.OpenAI]:
    def factory(api_key: str = TEST_API_KEY) -> openai.OpenAI:
        return openai.OpenAI(
            base_url="http://testserver/v1", api_key=api_key, http_client=client, max_retries=0
        )

    return factory


@pytest.fixture
def sdk(sdk_factory: Callable[..., openai.OpenAI]) -> openai.OpenAI:
    return sdk_factory()


@pytest.fixture
def model(driver_name: str) -> str:
    return DEFAULT_MODEL[driver_name]


def test_chat(sdk: openai.OpenAI, model: str, driver_name: str) -> None:
    completion = sdk.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
        ],
        temperature=0.2,
    )
    assert completion.object == "chat.completion"
    assert completion.model == model
    choice = completion.choices[0]
    assert choice.message.role == "assistant"
    assert choice.message.content == FAKE_REPLY_TEXT[driver_name]
    assert choice.finish_reason == "stop"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 20
    assert completion.usage.prompt_tokens_details is not None
    assert completion.usage.prompt_tokens_details.cached_tokens == 3


def test_stream(sdk: openai.OpenAI, model: str, driver_name: str) -> None:
    stream = sdk.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text = ""
    finish = None
    usage = None
    for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
            finish = chunk.choices[0].finish_reason or finish
        if chunk.usage:
            usage = chunk.usage
    assert text == FAKE_REPLY_TEXT[driver_name]
    assert finish == "stop"
    assert usage is not None
    assert usage.total_tokens == 20


def test_models(sdk: openai.OpenAI, model: str) -> None:
    ids = [m.id for m in sdk.models.list()]
    assert model in ids


def test_authentication_error(sdk_factory: Callable[..., openai.OpenAI], model: str) -> None:
    bad = sdk_factory(api_key="wrong-" + "x" * 40)
    with pytest.raises(openai.AuthenticationError) as info:
        bad.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}])
    assert info.value.code == "authentication_failed"


def test_model_not_found(sdk: openai.OpenAI) -> None:
    with pytest.raises(openai.NotFoundError) as info:
        sdk.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
    assert info.value.code == "model_not_found"
    assert info.value.param == "model"


def test_bad_request(sdk: openai.OpenAI, model: str) -> None:
    with pytest.raises(openai.BadRequestError) as info:
        sdk.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}], n=2)
    assert info.value.code == "invalid_request"


def test_rate_limit_and_server_errors(
    sdk: openai.OpenAI, model: str, driver_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[openai.types.chat.ChatCompletionMessageParam] = [
        {"role": "user", "content": "x"}
    ]
    use_fixture(monkeypatch, driver_name, "rate_limited.jsonl", 1)
    with pytest.raises(openai.RateLimitError) as limited:
        sdk.chat.completions.create(model=model, messages=messages)
    assert limited.value.code == "provider_rate_limited"
    use_fixture(monkeypatch, driver_name, "overloaded.jsonl", 1)
    with pytest.raises(openai.InternalServerError) as server:
        sdk.chat.completions.create(model=model, messages=messages)
    assert server.value.status_code == 503
    assert server.value.code == "provider_unavailable"


def test_error_after_stream_started(
    sdk: openai.OpenAI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver_name: str
) -> None:
    if driver_name != "claude":
        pytest.skip("only Claude streams text before the end of the turn")
    lines = [
        {"type": "stream_event",
         "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}},
        {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 529},
    ]  # fmt: skip
    fixture = tmp_path / "mid.jsonl"
    fixture.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    monkeypatch.setenv("FAKE_SCENARIO", "fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    stream = sdk.chat.completions.create(
        model="sonnet", messages=[{"role": "user", "content": "x"}], stream=True
    )
    with pytest.raises(openai.APIError, match="upstream provider is unavailable"):
        for _chunk in stream:
            pass
