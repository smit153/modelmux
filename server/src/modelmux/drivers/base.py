"""The driver plugin contract.

A driver translates between ModelMux and one CLI. It contains no HTTP logic
and never spawns processes itself: invocations are executed by the runtime,
and ``certify()`` / ``probe()`` run the CLI through the ``ProbeContext`` they
are given.

Checks are split by what they depend on:

- ``certify()`` runs at image build time (``python -m modelmux certify``) and
  checks what the pinned CLI binary decides: its version, every lockdown
  flag, and (when the binary decides them) its models. The result is stored
  in the image's manifest, bound to the binary's hash and the lockdown spec.
- ``probe()`` runs at every startup and checks only what can change: the
  login (one tiny live request) and, when they depend on the account, the
  models.

See docs/WRITING_A_DRIVER.md.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from modelmux.drivers.events import Completed, NormalizedEvent, ProviderFailure, ToolAttempt
from modelmux.errors import ModelMuxError
from modelmux.runtime.runner import Invocation, ProbeContext, ProbeOutput, run_argv

VERSION_BUDGET = 30.0
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


class CheckError(Exception):
    """A certify or probe check failed. The message is the short, safe reason."""


__all__ = [
    "Certified",
    "CertifyResult",
    "CheckError",
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
    """A public model ID and the CLI ``--model`` value it maps to (the allowlist).

    Drivers discover these from the CLI during ``probe()``; nothing is hardcoded.
    """

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
class CertifyResult:
    """Outcome of build-time certification. ``reason`` is short and safe to print.

    ``models`` holds the models when the CLI binary alone decides them, or
    ``None`` when they depend on the account and ``probe()`` discovers them.
    """

    ok: bool
    reason: str
    version: str | None = None
    models: tuple[ModelInfo, ...] | None = None


@dataclass(frozen=True)
class Certified:
    """What build-time certification established, handed to ``probe()``."""

    version: str
    models: tuple[ModelInfo, ...] | None


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a startup probe. ``reason`` is short and safe to log/print.

    On success ``models`` holds the models the CLI offers, as discovered from
    the CLI itself; they become the allowlist (optionally narrowed by
    MODELMUX_MODELS).
    """

    ok: bool
    reason: str
    version: str | None = None
    models: tuple[ModelInfo, ...] = ()


class Driver(ABC):
    """Base class for CLI drivers. One instance per process."""

    name: ClassVar[str]
    binary_name: ClassVar[str]
    supported_versions: ClassVar[str]  # PEP 440 specifier, e.g. ">=2.1,<3"

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    @abstractmethod
    async def certify(self, ctx: ProbeContext) -> CertifyResult:
        """Build time, no login: check the version and that every lockdown flag
        is accepted, and read the models if the binary alone decides them."""

    @abstractmethod
    async def probe(self, ctx: ProbeContext, certified: Certified) -> ProbeResult:
        """Startup: everything that can change. Return the models (the certified
        ones, or ones discovered now when they depend on the account) and make
        one live check. No fallbacks: if the models are unknown, fail."""

    @abstractmethod
    def lockdown_spec(self) -> tuple[str, ...]:
        """Every fixed argument and setting the driver relies on for lockdown.

        Its hash is stored at certification; a code change to the lockdown
        without a new certification stops startup.
        """

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

    def integrity_files(self) -> tuple[Path, ...]:
        """Files whose hashes bind the certification to this installation.

        By default the CLI binary; drivers whose binary is a launcher add the
        program it starts.
        """
        return (self.binary,)

    def argv(self, *args: str) -> tuple[str, ...]:
        return run_argv(self.binary, args)

    # ---------------------------------------------------------------- shared checks

    async def check_version(self, ctx: ProbeContext) -> str:
        """``<binary> --version`` must be in ``supported_versions``. Returns it."""
        out = await ctx.run(
            lambda _ws: Invocation(argv=self.argv("--version"), stdin=b""), budget=VERSION_BUDGET
        )
        match = _VERSION_RE.search(out.text())
        if out.exit_code != 0 or match is None:
            raise CheckError("could not read the CLI version")
        version = match.group(1)
        try:
            supported = Version(version) in SpecifierSet(self.supported_versions)
        except InvalidVersion:
            supported = False
        if not supported:
            raise CheckError(f"CLI version {version} is not in {self.supported_versions}")
        return version

    async def check_live(
        self, ctx: ProbeContext, build: Callable[[Path], Invocation], *, budget: float
    ) -> None:
        """One tiny real request: it must complete without tripping the wire."""
        out = await ctx.run(build, budget=budget)
        events = [event for line in out.lines for event in self.parse_line(line)]
        if any(isinstance(e, ToolAttempt) for e in events):
            raise CheckError("live check triggered the sandbox tripwire")
        failure = next((e for e in events if isinstance(e, ProviderFailure)), None)
        if failure is not None:
            raise CheckError(f"live check failed: {failure.kind}")
        if out.exit_code != 0 or not any(isinstance(e, Completed) for e in events):
            raise CheckError("live check did not complete")
