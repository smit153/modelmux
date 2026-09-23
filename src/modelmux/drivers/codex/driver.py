"""Codex driver: ``codex exec --json`` as a locked-down, text-only backend.

Verified against codex-cli 0.159.2 without an account: flags from
``codex exec --help``, features from ``codex features list``, config keys
from the official config reference. The probe checks all of them offline:

- an unknown flag fails with "unexpected argument", an unknown feature with
  "Unknown feature flag", and empty stdin with "No prompt provided";
- unknown ``-c`` keys are silently ignored by Codex, so each key is checked
  by passing an invalid value and expecting an error that names the key.

Credentials live under ``$HOME/.codex`` (HOME is the driver home); the
driver deliberately does not set ``CODEX_HOME``.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from functools import partial
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
from modelmux.drivers.codex import parser
from modelmux.drivers.events import (
    Completed,
    NormalizedEvent,
    ProviderFailure,
    ToolAttempt,
    sanitize_detail,
)
from modelmux.errors import ModelMuxError, ProviderAuthError, ProviderError

SYSTEM_PROMPT_FILE = "system-prompt.txt"

DEFAULT_MODELS = ("gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra")

# Every feature that is on by default in 0.159.2 and gives the agent a tool,
# a way to act, or outside input. Unknown names are rejected by the CLI.
DISABLED_FEATURES = (
    "apps", "browser_use", "browser_use_external", "code_mode_host", "computer_use",
    "daemon_auto_start", "goals", "hooks", "image_generation", "in_app_browser",
    "in_app_local_automation", "multi_agent", "plugins", "remote_plugin", "shell_snapshot",
    "shell_tool", "skill_mcp_dependency_install", "skill_search", "sleep_tool",
    "tool_call_mcp_elicitation", "tool_suggest", "unified_exec", "view_image",
    "workspace_dependencies", "worktrees",
)  # fmt: skip

# Fixed config overrides (key -> TOML value). The instructions file is added
# per request because its path is inside the private workspace.
CONFIG_OVERRIDES = (
    ("approval_policy", '"never"'),
    ("web_search", '"disabled"'),
    ("mcp_servers", "{}"),
    ("history.persistence", '"none"'),
    ("analytics.enabled", "false"),
    ("check_for_update_on_startup", "false"),
    ("project_doc_max_bytes", "0"),
    ("shell_environment_policy.inherit", '"none"'),
)
INSTRUCTIONS_KEY = "model_instructions_file"
VERIFIED_KEYS = (*(key for key, _ in CONFIG_OVERRIDES), INSTRUCTIONS_KEY)

PROBE_SYSTEM = "Reply with exactly the word: ok"
PROBE_PROMPT = "modelmux-probe: connectivity check. Reply with exactly the word: ok"
PROBE_BUDGET = 30.0
LIVE_PROBE_BUDGET = 120.0

NO_PROMPT = "No prompt provided"
CONFIG_ERROR = "Error loading config"
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_UNKNOWN_FLAG_RE = re.compile(r"unexpected argument '([^']{1,64})'")
_UNKNOWN_FEATURE_RE = re.compile(r"Unknown feature flag: (\S{1,64})")
_AUTH_HINTS = ("401", "unauthorized", "not logged in", "login")


def _fixed(invocation: Invocation, _workspace: Path) -> Invocation:
    return invocation


class CodexDriver(Driver):
    name: ClassVar[str] = "codex"
    binary_name: ClassVar[str] = "codex"
    supported_versions: ClassVar[str] = ">=0.159,<1.0"

    def models(self) -> list[ModelInfo]:
        return [ModelInfo(id=model, cli_model=model) for model in DEFAULT_MODELS]

    # ---------------------------------------------------------------- invocation

    def lockdown_args(self, cli_model: str, workspace: Path) -> tuple[str, ...]:
        args = [
            "exec",
            "--json",
            "--model", cli_model,
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color", "never",
            "-C", str(workspace),
        ]  # fmt: skip
        for key, value in CONFIG_OVERRIDES:
            args += ["-c", f"{key}={value}"]
        instructions = json.dumps(str(workspace / SYSTEM_PROMPT_FILE))  # a TOML basic string
        args += ["-c", f"{INSTRUCTIONS_KEY}={instructions}"]
        for feature in DISABLED_FEATURES:
            args += ["--disable", feature]
        args.append("-")  # read the prompt from stdin
        return tuple(args)

    def _invocation(self, cli_model: str, workspace: Path, system: str, stdin: bytes) -> Invocation:
        return Invocation(
            argv=self.argv(*self.lockdown_args(cli_model, workspace)),
            stdin=stdin,
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
        if "unexpected argument" in lowered or "unknown feature flag" in lowered:
            return ProviderError(f"cli rejected a flag (exit {exit_code})")
        if CONFIG_ERROR.lower() in lowered:
            return ProviderError(f"cli rejected its configuration (exit {exit_code})")
        if any(hint in lowered for hint in _AUTH_HINTS):
            return ProviderAuthError(f"cli reported an auth problem (exit {exit_code})")
        return None

    # ---------------------------------------------------------------- probe

    async def probe(self, ctx: ProbeContext) -> ProbeResult:
        try:
            version = await self._check_version(ctx)
            if isinstance(version, ProbeResult):
                return version
            for check in (self._check_flags, self._check_config_keys, self._check_live):
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
        """Every flag and feature name must be accepted. Empty stdin: no API call."""
        out = await ctx.run(
            lambda ws: self._invocation(DEFAULT_MODELS[0], ws, PROBE_SYSTEM, b""),
            budget=PROBE_BUDGET,
        )
        text = out.text()
        if unknown := _UNKNOWN_FLAG_RE.search(text):
            flag = sanitize_detail(unknown.group(1), 64)
            return ProbeResult(ok=False, reason=f"CLI does not support lockdown flag {flag}")
        if unknown := _UNKNOWN_FEATURE_RE.search(text):
            feature = sanitize_detail(unknown.group(1), 64)
            return ProbeResult(ok=False, reason=f"CLI does not know feature {feature}")
        if CONFIG_ERROR in text:
            return ProbeResult(ok=False, reason="CLI rejected the lockdown configuration")
        if NO_PROMPT not in text:
            return ProbeResult(ok=False, reason="lockdown flag check gave an unexpected response")
        return None

    async def _check_config_keys(self, ctx: ProbeContext) -> ProbeResult | None:
        """Codex ignores unknown -c keys, so prove each one is recognised."""
        for key in VERIFIED_KEYS:
            invocation = Invocation(
                argv=self.argv("exec", "--json", "--ignore-user-config", "-c", f"{key}=[1]", "-"),
                stdin=b"",
            )
            out = await ctx.run(partial(_fixed, invocation), budget=PROBE_BUDGET)
            text = out.text()
            if CONFIG_ERROR not in text or f"`{key}`" not in text:
                return ProbeResult(ok=False, reason=f"CLI does not recognise config key {key}")
        return None

    async def _check_live(self, ctx: ProbeContext) -> ProbeResult | None:
        """One tiny real request, confirming the CLI is logged in and working."""

        def build(ws: Path) -> Invocation:
            request = DriverRequest(DEFAULT_MODELS[0], PROBE_SYSTEM, PROBE_PROMPT, ws, "probe")
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
