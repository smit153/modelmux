"""A minimal driver used in registry tests (and as a documentation example).

It drives the fake CLI's ``echo`` scenario: every stdout line is assistant text.
"""

from __future__ import annotations

from typing import ClassVar

from modelmux.drivers.base import (
    Certified,
    CertifyResult,
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

    async def certify(self, ctx: ProbeContext) -> CertifyResult:
        models = (ModelInfo(id="echo-1", cli_model="echo-1"),)
        return CertifyResult(ok=True, reason="ok", version="1.0.0", models=models)

    async def probe(self, ctx: ProbeContext, certified: Certified) -> ProbeResult:
        return ProbeResult(ok=True, reason="ok", models=certified.models or ())

    def lockdown_spec(self) -> tuple[str, ...]:
        return ("--model", "<model>")

    def build_invocation(self, req: DriverRequest) -> Invocation:
        return Invocation(argv=self.argv("--model", req.cli_model), stdin=req.transcript.encode())

    def parse_line(self, line: bytes) -> list[NormalizedEvent]:
        return [TextFinal(line.decode("utf-8", "replace")), Completed()]

    def classify_exit(
        self, exit_code: int, stderr_tail: str, seen: list[NormalizedEvent]
    ) -> ModelMuxError | None:
        return None
