"""Orchestrates one chat completion end to end.

render prompt -> concurrency slot -> private workspace -> driver invocation
-> runtime run -> driver events -> tripwire / classification -> response.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from modelmux import __version__
from modelmux.api.schemas import (
    ChatCompletion,
    ChatCompletionRequest,
    Choice,
    ResponseMessage,
    stop_sequences,
)
from modelmux.config import Settings
from modelmux.core.prompt import RenderedPrompt, render_prompt
from modelmux.core.usage import merge_usage, to_openai_usage
from modelmux.drivers.base import Driver, DriverRequest, ModelInfo
from modelmux.drivers.events import (
    Completed,
    NormalizedEvent,
    ProviderFailure,
    TextDelta,
    TextFinal,
    ToolAttempt,
    UsageReport,
    failure_to_error,
)
from modelmux.errors import (
    ModelMuxError,
    ModelNotFoundError,
    ProtocolError,
    ProviderError,
    SandboxViolationError,
)
from modelmux.observability.logging import content_logging_enabled
from modelmux.runtime.limits import ConcurrencyLimiter
from modelmux.runtime.runner import Runner
from modelmux.runtime.workspace import create_workspace

log = logging.getLogger("modelmux.pipeline")


@dataclass
class RunOutcome:
    """What one CLI run produced, after classification."""

    text: str
    usage: list[UsageReport] = field(default_factory=list)


@dataclass
class _Collector:
    deltas: list[str] = field(default_factory=list)
    finals: list[str] = field(default_factory=list)
    completed: Completed | None = None
    usage: list[UsageReport] = field(default_factory=list)
    failure: ProviderFailure | None = None
    seen: list[NormalizedEvent] = field(default_factory=list)

    def add(self, event: NormalizedEvent) -> None:
        self.seen.append(event)
        match event:
            case TextDelta(text=text):
                self.deltas.append(text)
            case TextFinal(text=text):
                self.finals.append(text)
            case Completed():
                self.completed = event
            case UsageReport():
                self.usage.append(event)
            case ProviderFailure():
                if self.failure is None:
                    self.failure = event

    def text(self) -> str:
        if self.completed is not None and self.completed.final_text is not None:
            return self.completed.final_text
        return "".join(self.finals) or "".join(self.deltas)


@dataclass(frozen=True)
class CompletionResult:
    completion: ChatCompletion
    usage_available: bool


def apply_stop(text: str, stops: list[str]) -> tuple[str, bool]:
    """Truncate at the earliest stop sequence."""
    cut = min((i for s in stops if (i := text.find(s)) >= 0), default=-1)
    return (text[:cut], True) if cut >= 0 else (text, False)


def new_completion_id() -> str:
    return "chatcmpl-" + secrets.token_hex(12)


class Pipeline:
    def __init__(
        self,
        *,
        driver: Driver,
        runner: Runner,
        limiter: ConcurrencyLimiter,
        models: dict[str, ModelInfo],
        settings: Settings,
    ) -> None:
        self.driver = driver
        self.runner = runner
        self.limiter = limiter
        self.models = models
        self.settings = settings
        self.fingerprint = f"modelmux-{__version__}-{driver.name}"

    @property
    def work_root(self) -> Path:
        return self.settings.work_root

    def resolve_model(self, model_id: str) -> ModelInfo:
        model = self.models.get(model_id)
        if model is None:
            raise ModelNotFoundError(
                "model not in allowlist",
                message="The requested model does not exist or is not available.",
                param="model",
            )
        return model

    async def complete(self, req: ChatCompletionRequest, request_id: str) -> CompletionResult:
        model = self.resolve_model(req.model)
        stops = stop_sequences(req, self.settings.max_stop_sequences)
        rendered = render_prompt(req.messages, max_bytes=self.settings.max_prompt_bytes)
        if content_logging_enabled():
            log.debug(
                "prompt",
                extra={
                    "event": "content",
                    "system": rendered.system,
                    "prompt": rendered.transcript,
                },
            )

        async with self.limiter.slot():
            outcome = await self.run_once(model, rendered, request_id)

        text, _stopped = apply_stop(outcome.text, stops)
        report = merge_usage(outcome.usage)
        if content_logging_enabled():
            log.debug("completion", extra={"event": "content", "completion": text})
        completion = ChatCompletion(
            id=new_completion_id(),
            created=int(time.time()),
            model=req.model,
            system_fingerprint=self.fingerprint,
            choices=[Choice(message=ResponseMessage(content=text), finish_reason="stop")],
            usage=to_openai_usage(report),
        )
        return CompletionResult(completion, usage_available=report is not None)

    async def run_once(
        self,
        model: ModelInfo,
        rendered: RenderedPrompt,
        request_id: str,
        *,
        deadline: float | None = None,
    ) -> RunOutcome:
        """One CLI run in a fresh workspace. Raises the classified error on failure."""
        collector = _Collector()
        with create_workspace(self.work_root) as workspace:
            invocation = self.driver.build_invocation(
                DriverRequest(
                    cli_model=model.cli_model,
                    system_prompt=rendered.system,
                    transcript=rendered.transcript,
                    workspace=workspace.path,
                    request_id=request_id,
                )
            )
            async with self.runner.start(invocation, workspace, deadline=deadline) as run:
                async for line in run:
                    for event in self.driver.parse_line(line):
                        if isinstance(event, ToolAttempt):
                            await run.kill("sandbox_violation")
                            log.error(
                                "sandbox violation",
                                extra={
                                    "event": "sandbox_violation",
                                    "kind": event.kind,
                                    "detail": event.detail,
                                },
                            )
                            raise SandboxViolationError(f"{event.kind}: {event.detail}")
                        collector.add(event)
            result = run.result

        error = self._classify(result.exit_code, result.stderr_tail, collector)
        log.info(
            "cli run finished",
            extra={
                "event": "cli_run",
                "exit_code": result.exit_code,
                "duration_ms": round(result.duration * 1000, 1),
                "stdout_bytes": result.stdout_bytes,
                "prompt_bytes": rendered.size,
                "outcome": error.code if error else "ok",
            },
        )
        if error is not None:
            error.internal_detail = (
                f"{error.internal_detail}; exit={result.exit_code}; "
                f"stderr_tail={result.stderr_tail[-500:]!r}"
            )
            raise error
        return RunOutcome(text=collector.text(), usage=collector.usage)

    def _classify(self, exit_code: int, stderr_tail: str, c: _Collector) -> ModelMuxError | None:
        """Section 13.2 order (runtime errors and violations were already raised)."""
        if c.failure is not None:
            return failure_to_error(c.failure)
        if exit_code != 0:
            return self.driver.classify_exit(exit_code, stderr_tail, c.seen) or ProviderError(
                f"cli exited with {exit_code}"
            )
        if c.completed is None:
            return ProtocolError("cli exited 0 without a completion event")
        return None
