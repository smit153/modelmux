"""Claude Code driver: ``claude -p`` as a locked-down, text-only backend.

Verified against Claude Code 2.1.285. ``--max-turns`` and
``--system-prompt-file`` are accepted but not listed in ``--help``, so the
probe checks that the CLI *accepts* every lockdown flag (an unknown flag
fails with "unknown option" before any input is read) instead of grepping
``--help``. No API call is made by that check.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from modelmux.drivers.base import (
    Driver,
    DriverRequest,
    Invocation,
    ModelInfo,
    ProbeContext,
    ProbeResult,
)
from modelmux.drivers.claude import parser
from modelmux.drivers.events import (
    Completed,
    NormalizedEvent,
    ProviderFailure,
    ToolAttempt,
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

DEFAULT_MODELS = (
    ("sonnet", "sonnet"),
    ("opus", "opus"),
    ("haiku", "haiku"),
    ("fable", "fable"),
    ("claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("claude-opus-5-5", "claude-opus-5-5"),
    ("claude-fable-5-1", "claude-fable-5-1"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
)

# Startup login check: one tiny real request on this model.
PROBE_MODEL = "sonnet"
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

    def models(self) -> list[ModelInfo]:
        return [ModelInfo(id=public, cli_model=cli) for public, cli in DEFAULT_MODELS]

    def env_allowlist(self) -> frozenset[str]:
        return frozenset({"DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"})

    # ---------------------------------------------------------------- invocation

    def lockdown_args(self, cli_model: str, system_prompt_path: Path) -> tuple[str, ...]:
        return (
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", cli_model,
            "--max-turns", "1",
            "--system-prompt-file", str(system_prompt_path),
            "--tools", "",
            "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            "--setting-sources", "",
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--safe-mode",
            "--restricted",
            "--disable-slash-commands",
            "--permission-mode", "dontAsk",
            "--permission-prompts", "none",
            "--no-session-persistence",
        )  # fmt: skip

    def _invocation(self, cli_model: str, workspace: Path, system: str, stdin: bytes) -> Invocation:
        return Invocation(
            argv=self.argv(*self.lockdown_args(cli_model, workspace / SYSTEM_PROMPT_FILE)),
            stdin=stdin,
            env={"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
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

    async def probe(self, ctx: ProbeContext) -> ProbeResult:
        try:
            version = await self._check_version(ctx)
            if isinstance(version, ProbeResult):
                return version
            for check in (self._check_flags, self._check_live):
                failed = await check(ctx)
                if failed is not None:
                    return replace(failed, version=version)
        except ModelMuxError as exc:
            return ProbeResult(ok=False, reason=f"probe run failed: {exc.code}")
        return ProbeResult(ok=True, reason="ok", version=version)

    async def _check_version(self, ctx: ProbeContext) -> str | ProbeResult:
        out = await ctx.run(
            lambda _ws: Invocation(argv=self.argv("--version"), stdin=b""), budget=PROBE_BUDGET
        )
        match = _VERSION_RE.search(out.text())
        if out.exit_code != 0 or match is None:
            return ProbeResult(ok=False, reason="could not read the CLI version")
        version = match.group(1)
        try:
            supported = Version(version) in SpecifierSet(self.supported_versions)
        except InvalidVersion:
            supported = False
        if not supported:
            return ProbeResult(
                ok=False,
                reason=f"CLI version {version} is not in {self.supported_versions}",
                version=version,
            )
        return version

    async def _check_flags(self, ctx: ProbeContext) -> ProbeResult | None:
        """Every lockdown flag must be accepted. Empty stdin: no API call is made."""
        out = await ctx.run(
            lambda ws: self._invocation(PROBE_MODEL, ws, PROBE_SYSTEM, b""), budget=PROBE_BUDGET
        )
        text = out.text()
        unknown = _UNKNOWN_OPTION_RE.search(text)
        if unknown:
            flag = sanitize_detail(unknown.group(1), 64)
            return ProbeResult(ok=False, reason=f"CLI does not support lockdown flag {flag}")
        if INPUT_REQUIRED not in text:
            return ProbeResult(ok=False, reason="lockdown flag check gave an unexpected response")
        return None

    async def _check_live(self, ctx: ProbeContext) -> ProbeResult | None:
        """One tiny real request, confirming the CLI is logged in and working."""

        def build(ws: Path) -> Invocation:
            request = DriverRequest(PROBE_MODEL, PROBE_SYSTEM, PROBE_PROMPT, ws, "probe")
            return self.build_invocation(request)

        out = await ctx.run(build, budget=LIVE_PROBE_BUDGET)
        events = [event for line in out.lines for event in self.parse_line(line)]
        if any(isinstance(e, ToolAttempt) for e in events):
            return ProbeResult(ok=False, reason="live check triggered the sandbox tripwire")
        failure = next((e for e in events if isinstance(e, ProviderFailure)), None)
        if failure is not None:
            return ProbeResult(ok=False, reason=f"live check failed: {failure.kind}")
        if out.exit_code != 0 or not any(isinstance(e, Completed) for e in events):
            return ProbeResult(ok=False, reason="live check did not complete")
        return None
