"""Orchestrates one chat completion end to end.

render prompt -> concurrency slot -> private workspace -> driver invocation
-> runtime run -> driver events -> tripwire / classification -> response.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path

from modelmux import __version__
from modelmux.api.schemas import (
    ChatCompletion,
    ChatCompletionRequest,
    Choice,
    ResponseMessage,
    ToolCall,
    Usage,
    stop_sequences,
)
from modelmux.config import Settings
from modelmux.core.output import Answer, Invalid, interpret, repair_messages
from modelmux.core.prompt import RenderedPrompt, render_prompt
from modelmux.core.streaming import StopScanner
from modelmux.core.structured import build_format, format_instructions
from modelmux.core.tools import build_tool_policy, tool_instructions
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
    ContextTooLargeError,
    InvalidModelOutputError,
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
class TextPiece:
    text: str


@dataclass(frozen=True)
class ToolCallsPiece:
    calls: list[ToolCall]


@dataclass(frozen=True)
class StreamEnd:
    finish_reason: str
    usage: Usage
    usage_available: bool


StreamPiece = TextPiece | ToolCallsPiece | StreamEnd


def needs_buffering(req: ChatCompletionRequest) -> bool:
    fmt = req.response_format
    return bool(req.tools) or (fmt is not None and fmt.type != "text")


@dataclass(frozen=True)
class CompletionResult:
    completion: ChatCompletion
    usage_available: bool


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
        tools = build_tool_policy(req)
        fmt = build_format(req)
        sections = [tool_instructions(tools)] if tools else []
        if fmt:
            sections.append(format_instructions(fmt))
        rendered = render_prompt(
            req.messages, sections=sections, max_bytes=self.settings.max_prompt_bytes
        )
        self._log_content("prompt", system=rendered.system, prompt=rendered.transcript)

        def judge(text: str) -> Answer | Invalid:
            return interpret(
                text,
                tools=tools,
                fmt=fmt,
                stops=stops,
                max_args_bytes=self.settings.max_tool_arguments_bytes,
            )

        async with self.limiter.slot():
            # One budget for the run and its repair.
            deadline = asyncio.get_running_loop().time() + self.settings.total_timeout
            outcome = await self.run_once(model, rendered, request_id, deadline=deadline)
            usage = list(outcome.usage)
            verdict = judge(outcome.text)
            if isinstance(verdict, Invalid):
                repaired = await self._repair(
                    req,
                    model=model,
                    sections=sections,
                    bad_output=outcome.text,
                    verdict=verdict,
                    request_id=request_id,
                    deadline=deadline,
                )
                usage += repaired.usage
                verdict = judge(repaired.text)
                if isinstance(verdict, Invalid):
                    # No reason in the detail: it can quote model output.
                    raise InvalidModelOutputError("output still invalid after one repair")

        report = merge_usage(usage)
        self._log_content("completion", completion=verdict.content or "")
        message = ResponseMessage(content=verdict.content, tool_calls=verdict.tool_calls or None)
        completion = ChatCompletion(
            id=new_completion_id(),
            created=int(time.time()),
            model=req.model,
            system_fingerprint=self.fingerprint,
            choices=[Choice(message=message, finish_reason=verdict.finish_reason)],
            usage=to_openai_usage(report),
        )
        return CompletionResult(completion, usage_available=report is not None)

    async def _repair(
        self,
        req: ChatCompletionRequest,
        *,
        model: ModelInfo,
        sections: list[str],
        bad_output: str,
        verdict: Invalid,
        request_id: str,
        deadline: float,
    ) -> RunOutcome:
        """Exactly one corrective re-run within the same deadline (plan 8.3)."""
        log.warning("invalid model output, repairing", extra={"event": "repair"})
        messages = repair_messages(req.messages, bad_output, verdict.error)
        try:
            rendered = render_prompt(
                messages, sections=sections, max_bytes=self.settings.max_prompt_bytes
            )
        except ContextTooLargeError:
            raise InvalidModelOutputError("repair prompt exceeds the prompt limit") from None
        return await self.run_once(model, rendered, request_id, deadline=deadline)

    def _log_content(self, what: str, **fields: str) -> None:
        if content_logging_enabled():
            log.debug(what, extra={"event": "content", **fields})

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
        async with aclosing(self._execute(model, rendered, request_id, collector, deadline)) as it:
            async for _event in it:
                pass
        return RunOutcome(text=collector.text(), usage=collector.usage)

    async def stream(
        self, req: ChatCompletionRequest, request_id: str
    ) -> AsyncGenerator[StreamPiece, None]:
        """Yield text as it arrives, then one ``StreamEnd``.

        Requests with tools or structured output are buffered: the validated
        answer is produced first, then yielded as one piece.

        Errors before the end are raised from the generator. Closing the
        generator early (client gone) kills the CLI and releases the slot.
        """
        if needs_buffering(req):
            # Tools / structured output must be validated before anything is sent.
            result = await self.complete(req, request_id)
            choice = result.completion.choices[0]
            if choice.message.tool_calls:
                yield ToolCallsPiece(choice.message.tool_calls)
            elif choice.message.content:
                yield TextPiece(choice.message.content)
            yield StreamEnd(choice.finish_reason, result.completion.usage, result.usage_available)
            return

        model = self.resolve_model(req.model)
        stops = stop_sequences(req, self.settings.max_stop_sequences)
        rendered = render_prompt(req.messages, max_bytes=self.settings.max_prompt_bytes)
        scanner = StopScanner(stops)
        collector = _Collector()
        streamed_deltas = False
        async with self.limiter.slot():
            events = self._execute(model, rendered, request_id, collector, None)
            async with aclosing(events) as it:
                async for event in it:
                    if isinstance(event, TextDelta) and event.text:
                        streamed_deltas = True
                        if text := scanner.feed(event.text):
                            yield TextPiece(text)
                        if scanner.stopped:
                            break  # closing the run kills the CLI
            # Drivers without token deltas (e.g. Codex) deliver the whole message at once.
            if not streamed_deltas and (text := scanner.feed(collector.text())):
                yield TextPiece(text)
            if tail := scanner.flush():
                yield TextPiece(tail)
        report = merge_usage(collector.usage)
        yield StreamEnd("stop", to_openai_usage(report), usage_available=report is not None)

    async def _execute(
        self,
        model: ModelInfo,
        rendered: RenderedPrompt,
        request_id: str,
        collector: _Collector,
        deadline: float | None,
    ) -> AsyncGenerator[NormalizedEvent, None]:
        """Run the CLI once, yielding events; raise the classified error at the end."""
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
                        yield event
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
