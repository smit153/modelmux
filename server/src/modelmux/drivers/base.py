"""The driver plugin contract.

A driver translates between ModelMux and one CLI. It contains no HTTP logic
and never spawns processes itself: invocations are executed by the runtime,
and ``probe()`` runs the CLI through the ``ProbeContext`` it is given.

See docs/WRITING_A_DRIVER.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from modelmux.drivers.events import NormalizedEvent
from modelmux.errors import ModelMuxError
from modelmux.runtime.runner import Invocation, ProbeContext, ProbeOutput, run_argv

__all__ = [
    "Driver",
    "DriverRequest",
    "Invocation",
    "ModelInfo",
    "ProbeContext",
    "ProbeOutput",
    "ProbeResult",
    "run_argv",
]


@dataclass(frozen=True)
class ModelInfo:
    """A public model ID and the CLI ``--model`` value it maps to (the allowlist)."""

    id: str
    cli_model: str


@dataclass(frozen=True)
class DriverRequest:
    """Everything a driver needs to build one invocation.

    ``cli_model`` always comes from the allowlist, never from raw request data.
    ``system_prompt`` and ``transcript`` are untrusted text: pass them via
    stdin or private workspace files, never as argv.
    """

    cli_model: str
    system_prompt: str
    transcript: str
    workspace: Path
    request_id: str


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a startup probe. ``reason`` is short and safe to log/print."""

    ok: bool
    reason: str
    version: str | None = None


class Driver(ABC):
    """Base class for CLI drivers. One instance per process."""

    name: ClassVar[str]
    binary_name: ClassVar[str]
    supported_versions: ClassVar[str]  # PEP 440 specifier, e.g. ">=2.1,<3"

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    @abstractmethod
    def models(self) -> list[ModelInfo]:
        """The default model allowlist (overridable with MODELMUX_MODELS)."""

    @abstractmethod
    async def probe(self, ctx: ProbeContext) -> ProbeResult:
        """Check version support and that every lockdown flag is accepted."""

    @abstractmethod
    def build_invocation(self, req: DriverRequest) -> Invocation:
        """argv (argv[0] = ``self.binary``), extra env, stdin bytes, private files."""

    @abstractmethod
    def parse_line(self, line: bytes) -> list[NormalizedEvent]:
        """Translate one stdout line. Must never raise on malformed input."""

    @abstractmethod
    def classify_exit(
        self, exit_code: int, stderr_tail: str, seen: list[NormalizedEvent]
    ) -> ModelMuxError | None:
        """Map a non-zero exit into an error, or ``None`` for the generic default."""

    def env_allowlist(self) -> frozenset[str]:
        """Env var names this driver may set or pass through (beyond the core set)."""
        return frozenset()

    def argv(self, *args: str) -> tuple[str, ...]:
        return run_argv(self.binary, args)
