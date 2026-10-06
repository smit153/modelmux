"""Claude Code driver: ``claude -p`` as a locked-down, text-only backend.

Verified against Claude Code 2.1.285. ``--max-turns`` and
``--system-prompt-file`` are accepted but not listed in ``--help``, so the
probe checks that the CLI *accepts* every lockdown flag (an unknown flag
fails with "unknown option" before any input is read) instead of grepping
``--help``. No API call is made by that check.

Models are discovered, not hardcoded: ``/model`` is answered locally by the
CLI (no API call, zero cost) with the list of aliases, and the init event of
the same query shows the full model ID an alias resolves to.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from modelmux.drivers.base import (
    Certified,
    CertifyResult,
    CheckError,
    Driver,
    DriverRequest,
    Invocation,
    ModelInfo,
    ProbeContext,
    ProbeResult,
)
from modelmux.drivers.claude import parser
from modelmux.drivers.events import (
    NormalizedEvent,
    sanitize_detail,
)
from modelmux.errors import ModelMuxError, ProviderAuthError, ProviderError

SYSTEM_PROMPT_FILE = "system-prompt.txt"

# Every built-in tool, denied by name as defence in depth on top of --tools "".
# Includes names from older versions; unknown names are accepted by the CLI.
DISALLOWED_TOOLS = (
    "Agent", "Bash", "BashOutput", "CronCreate", "CronDelete", "CronList", "DesignSync",
    "Edit", "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Glob", "Grep", "KillShell",
    "LS", "ListAgents", "ListMcpResourcesTool", "Monitor", "MultiEdit", "NotebookEdit",
    "PushNotification", "Read", "ReadMcpResourceTool", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "Skill", "SlashCommand", "Task", "TaskStop",
    "TodoWrite", "ToolSearch", "WebFetch", "WebSearch", "Write",
)  # fmt: skip

EMPTY_MCP_CONFIG = json.dumps({"mcpServers": {}})

SLASH_COMMANDS_FLAG = "--disable-slash-commands"
MODEL_QUERY = b"/model\n"
# /model queries resolving aliases run this many at a time (each is a Node process).
DISCOVERY_PARALLELISM = 2

# Startup login check: one tiny real request on the account's default model.
PROBE_SYSTEM = "Reply with exactly the word: ok"
PROBE_PROMPT = "modelmux-probe: connectivity check. Reply with exactly the word: ok"
PROBE_BUDGET = 30.0
LIVE_PROBE_BUDGET = 120.0

INPUT_REQUIRED = "Input must be provided"
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_UNKNOWN_OPTION_RE = re.compile(r"unknown option '([^']{1,64})'")
_AUTH_HINTS = ("invalid api key", "/login", "not logged in", "authentication")


class ClaudeDriver(Driver):
    name: ClassVar[str] = "claude"
    binary_name: ClassVar[str] = "claude"
    supported_versions: ClassVar[str] = ">=2.1,<3"

    def env_allowlist(self) -> frozenset[str]:
        return frozenset(self._env())

    @staticmethod
    def _env() -> dict[str, str]:
        return {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

    # ---------------------------------------------------------------- invocation

    def lockdown_args(self, cli_model: str | None, system_prompt_path: Path) -> tuple[str, ...]:
        """Every flag of a real request. ``cli_model`` None means the CLI's default."""
        model = ("--model", cli_model) if cli_model is not None else ()
        return (
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            *model,
            "--max-turns", "1",
            "--system-prompt-file", str(system_prompt_path),
            "--tools", "",
            "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            "--setting-sources", "",
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--safe-mode",
            "--restricted",
            SLASH_COMMANDS_FLAG,
            "--permission-mode", "dontAsk",
            "--permission-prompts", "none",
            "--no-session-persistence",
        )  # fmt: skip

    def _invocation(
        self, cli_model: str | None, workspace: Path, system: str, stdin: bytes
    ) -> Invocation:
        return Invocation(
            argv=self.argv(*self.lockdown_args(cli_model, workspace / SYSTEM_PROMPT_FILE)),
            stdin=stdin,
            env=self._env(),
            files={SYSTEM_PROMPT_FILE: system.encode()},
        )

    def build_invocation(self, req: DriverRequest) -> Invocation:
        return self._invocation(
            req.cli_model, req.workspace, req.system_prompt, req.transcript.encode()
        )

    # ---------------------------------------------------------------- output

    def parse_line(self, line: bytes) -> list[NormalizedEvent]:
        return parser.parse_line(line)

    def classify_exit(
        self, exit_code: int, stderr_tail: str, seen: list[NormalizedEvent]
    ) -> ModelMuxError | None:
        lowered = stderr_tail.lower()
        if "unknown option" in lowered:
            return ProviderError(f"cli rejected a flag (exit {exit_code})")
        if any(hint in lowered for hint in _AUTH_HINTS):
            return ProviderAuthError(f"cli reported an auth problem (exit {exit_code})")
        return None

    # ---------------------------------------------------------------- probe

    def lockdown_spec(self) -> tuple[str, ...]:
        args = self.lockdown_args("<model>", Path("<workspace>") / SYSTEM_PROMPT_FILE)
        env = (f"{k}={v}" for k, v in sorted(self._env().items()))
        return (*args, *env, f"query={MODEL_QUERY!r}", f"discovery-drops={SLASH_COMMANDS_FLAG}")

    # ---------------------------------------------------------------- certify (build time)

    async def certify(self, ctx: ProbeContext) -> CertifyResult:
        """Version, every lockdown flag, and the models: all decided by the binary.

        ``/model`` is answered locally without a login, so the model list is
        read here once instead of at every startup.
        """
        version = None
        try:
            version = await self.check_version(ctx)
            await self._check_flags(ctx)
            models = await self._discover_models(ctx)
        except CheckError as exc:
            return CertifyResult(ok=False, reason=str(exc), version=version)
        except ModelMuxError as exc:
            return CertifyResult(ok=False, reason=f"run failed: {exc.code}", version=version)
        return CertifyResult(ok=True, reason="ok", version=version, models=models)

    async def _check_flags(self, ctx: ProbeContext) -> None:
        """Every lockdown flag must be accepted. Empty stdin: no API call is made."""
        out = await ctx.run(
            lambda ws: self._invocation(None, ws, PROBE_SYSTEM, b""), budget=PROBE_BUDGET
        )
        text = out.text()
        unknown = _UNKNOWN_OPTION_RE.search(text)
        if unknown:
            flag = sanitize_detail(unknown.group(1), 64)
            raise CheckError(f"CLI does not support lockdown flag {flag}")
        if INPUT_REQUIRED not in text:
            raise CheckError("lockdown flag check gave an unexpected response")

    async def _query_model(self, ctx: ProbeContext, alias: str | None) -> parser.ModelReport:
        """Ask the CLI ``/model`` with every lockdown flag except the one that
        disables slash commands. Answered locally: no API call, zero cost.
        Build time only: the running server never enables slash commands."""

        def build(ws: Path) -> Invocation:
            invocation = self._invocation(alias, ws, PROBE_SYSTEM, MODEL_QUERY)
            argv = tuple(arg for arg in invocation.argv if arg != SLASH_COMMANDS_FLAG)
            return replace(invocation, argv=argv)

        out = await ctx.run(build, budget=PROBE_BUDGET)
        report = parser.parse_model_report(out.lines)
        if isinstance(report, str):
            raise CheckError(report)
        if out.exit_code != 0:
            raise CheckError("model query did not complete")
        return report

    async def _discover_models(self, ctx: ProbeContext) -> tuple[ModelInfo, ...]:
        """The aliases the CLI lists, each with the full ID it resolves to.

        Aliases with a context-size suffix (``sonnet[1m]``) are skipped, and
        only the first alias for each full ID is kept, so duplicates such as
        ``default`` or ``best`` drop out.
        """
        listing = await self._query_model(ctx, None)
        aliases = parser.parse_model_list(listing.text)
        if not aliases:
            raise CheckError("could not read the model list")
        candidates = [alias for alias in aliases if "[" not in alias]
        gate = asyncio.Semaphore(DISCOVERY_PARALLELISM)

        async def resolve(alias: str) -> parser.ModelReport:
            async with gate:
                return await self._query_model(ctx, alias)

        reports = await asyncio.gather(*(resolve(alias) for alias in candidates))
        published: dict[str, str] = {}  # full ID -> alias
        for alias, report in zip(candidates, reports, strict=True):
            published.setdefault(report.model, alias)
        aliases_kept = list(published.values())
        full_ids = [full for full in published if full not in aliases_kept]
        return tuple(ModelInfo(id=name, cli_model=name) for name in aliases_kept + full_ids)

    # ---------------------------------------------------------------- probe (startup)

    async def probe(self, ctx: ProbeContext, certified: Certified) -> ProbeResult:
        """The models come from certification; only the login is checked here.

        The live request passes no ``--model``, so the CLI uses the account's
        own default model and nothing about it has to be known in advance.
        """
        if not certified.models:
            return ProbeResult(ok=False, reason="the manifest lists no Claude models")
        try:
            await self.check_live(
                ctx,
                lambda ws: self._invocation(None, ws, PROBE_SYSTEM, PROBE_PROMPT.encode()),
                budget=LIVE_PROBE_BUDGET,
            )
        except CheckError as exc:
            return ProbeResult(ok=False, reason=str(exc))
        except ModelMuxError as exc:
            return ProbeResult(ok=False, reason=f"probe run failed: {exc.code}")
        return ProbeResult(ok=True, reason="ok", version=certified.version, models=certified.models)
