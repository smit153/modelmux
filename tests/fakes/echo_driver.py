"""A minimal driver used in registry tests (and as a documentation example).

It drives the fake CLI's ``echo`` scenario: every stdout line is assistant text.
"""

from __future__ import annotations

from typing import ClassVar

from modelmux.drivers.base import (
    Driver,
    DriverRequest,
    Invocation,
    ModelInfo,
    ProbeContext,
    ProbeResult,
)
from modelmux.drivers.events import Completed, NormalizedEvent, TextFinal
from modelmux.errors import ModelMuxError


class EchoDriver(Driver):
    name: ClassVar[str] = "echo"
    binary_name: ClassVar[str] = "fake-cli"
    supported_versions: ClassVar[str] = ">=0"

    def models(self) -> list[ModelInfo]:
        return [ModelInfo(id="echo-1", cli_model="echo-1")]

    async def probe(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(ok=True, reason="ok")

    def build_invocation(self, req: DriverRequest) -> Invocation:
        return Invocation(argv=self.argv("--model", req.cli_model), stdin=req.transcript.encode())

    def parse_line(self, line: bytes) -> list[NormalizedEvent]:
        return [TextFinal(line.decode("utf-8", "replace")), Completed()]

    def classify_exit(
        self, exit_code: int, stderr_tail: str, seen: list[NormalizedEvent]
    ) -> ModelMuxError | None:
        return None
