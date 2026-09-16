from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modelmux import errors as e
from modelmux.api.schemas import ChatCompletionRequest
from modelmux.config import Settings
from modelmux.core.pipeline import Pipeline
from modelmux.drivers.claude.driver import ClaudeDriver
from modelmux.drivers.registry import resolve_models
from modelmux.runtime.limits import ConcurrencyLimiter
from modelmux.runtime.runner import RunLimits, Runner, resolve_binary
from modelmux.runtime.workspace import prepare_work_root
from tests.fakes import FAKE_ENV_KEYS, FIXTURES

CLAUDE_FIXTURES = FIXTURES / "claude"


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


def build_pipeline(settings: Settings, max_concurrent: int = 2) -> Pipeline:
    assert settings.cli_path is not None
    driver = ClaudeDriver(resolve_binary("claude", settings.cli_path))
    prepare_work_root(settings.work_root)
    runner = Runner(
        driver.binary,
        home=settings.driver_home,
        env_allowlist=driver.env_allowlist() | FAKE_ENV_KEYS,
        limits=RunLimits.from_settings(settings),
    )
    return Pipeline(
        driver=driver,
        runner=runner,
        limiter=ConcurrencyLimiter(max_concurrent, 4, 5.0),
        models=resolve_models(driver, None),
        settings=settings,
    )


@pytest.fixture
def pipeline(settings: Settings) -> Pipeline:
    return build_pipeline(settings)


def chat(content: str = "Hi", **kwargs: Any) -> ChatCompletionRequest:
    body: dict[str, Any] = {"model": "sonnet", "messages": [{"role": "user", "content": content}]}
    body.update(kwargs)
    return ChatCompletionRequest.model_validate(body)


def use_fixture(monkeypatch: pytest.MonkeyPatch, name: str, exit_code: int = 0) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", "claude_fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(CLAUDE_FIXTURES / name))
    monkeypatch.setenv("FAKE_EXIT", str(exit_code))


def workspaces(settings: Settings) -> list[Path]:
    return list(settings.work_root.iterdir())


async def test_text_completion(pipeline: Pipeline, settings: Settings) -> None:
    result = await pipeline.complete(chat(), "req_1")
    c = result.completion
    assert c.id.startswith("chatcmpl-")
    assert len(c.id) == len("chatcmpl-") + 24
    assert c.model == "sonnet"
    assert c.system_fingerprint.endswith("-claude")
    assert c.choices[0].message.content == "Hello from fake Claude."
    assert c.choices[0].finish_reason == "stop"
    assert c.usage.prompt_tokens == 10 + 3 + 2
    assert c.usage.completion_tokens == 5
    assert c.usage.total_tokens == 20
    assert c.usage.prompt_tokens_details.cached_tokens == 3
    assert result.usage_available
    assert workspaces(settings) == []


async def test_no_usage(pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NO_USAGE", "1")
    result = await pipeline.complete(chat(), "req_1")
    assert not result.usage_available
    assert result.completion.usage.total_tokens == 0


async def test_prompt_reaches_cli_via_stdin(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REPLY", "__STDIN__")
    result = await pipeline.complete(
        chat(messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "Q?"}]),
        "req_1",
    )
    transcript = result.completion.choices[0].message.content or ""
    assert ":user>>\nQ?\n<</MMX-" in transcript
    assert "SYS" not in transcript  # system goes to the private file, not stdin


async def test_stop_sequence(pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_REPLY", "one two STOP three END")
    result = await pipeline.complete(chat(stop=["END", "STOP"]), "req_1")
    assert result.completion.choices[0].message.content == "one two "


async def test_recorded_fixture(pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fixture(monkeypatch, "text_partial.jsonl")
    result = await pipeline.complete(chat(), "req_1")
    assert result.completion.choices[0].message.content == "hello"
    assert result.completion.usage.prompt_tokens == 479


@pytest.mark.parametrize(
    ("fixture", "exit_code", "cls"),
    [
        ("rate_limited.jsonl", 1, e.ProviderRateLimitedError),
        ("rate_limit_rejected.jsonl", 1, e.ProviderRateLimitedError),
        ("auth_error.jsonl", 1, e.ProviderAuthError),
        ("overloaded.jsonl", 1, e.ProviderUnavailableError),
        ("context_too_long.jsonl", 1, e.ContextTooLargeError),
        ("model_not_found.jsonl", 1, e.ModelNotFoundError),
        ("malformed.jsonl", 0, e.ProtocolError),
        ("no_completion.jsonl", 0, e.ProtocolError),
        ("text_partial.jsonl", 2, e.ProviderError),  # completed, but non-zero exit
    ],
)
async def test_error_classification(
    pipeline: Pipeline,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    exit_code: int,
    cls: type[e.ModelMuxError],
) -> None:
    use_fixture(monkeypatch, fixture, exit_code)
    with pytest.raises(cls) as info:
        await pipeline.complete(chat(), "req_1")
    assert type(info.value) is cls
    assert workspaces(settings) == []


@pytest.mark.parametrize(
    "fixture",
    ["tool_use_read.jsonl", "init_with_tools.jsonl", "hook_event.jsonl",
     "unknown_exec_event.jsonl", "subagent_event.jsonl"],
)  # fmt: skip
async def test_tripwire_kills_fast(
    pipeline: Pipeline, settings: Settings, monkeypatch: pytest.MonkeyPatch, fixture: str
) -> None:
    use_fixture(monkeypatch, fixture)
    monkeypatch.setenv("FAKE_HANG_AFTER", "1")  # would run for an hour if not killed
    started = time.monotonic()
    with pytest.raises(e.SandboxViolationError):
        await pipeline.complete(chat(), "req_1")
    assert time.monotonic() - started < 1.0
    assert workspaces(settings) == []


async def test_unknown_model_before_spawn(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "spawned.json"
    monkeypatch.setenv("FAKE_OUT", str(out))
    with pytest.raises(e.ModelNotFoundError) as info:
        await pipeline.complete(chat(model="--help"), "req_1")
    assert info.value.param == "model"
    assert not out.exists()


async def test_prompt_limit_before_spawn(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pipe = build_pipeline(make_settings(max_prompt_bytes=2000))
    out = tmp_path / "spawned.json"
    monkeypatch.setenv("FAKE_OUT", str(out))
    with pytest.raises(e.ContextTooLargeError):
        await pipe.complete(chat("x" * 5000), "req_1")
    assert not out.exists()


async def test_runtime_timeout_propagates(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(first_output_timeout=0.3)
    pipe = build_pipeline(settings)
    monkeypatch.setenv("FAKE_SCENARIO", "hang_before_output")
    with pytest.raises(e.ProviderTimeoutError):
        await pipe.complete(chat(), "req_1")
    assert list(settings.work_root.iterdir()) == []
