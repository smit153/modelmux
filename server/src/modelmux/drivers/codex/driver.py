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

Models are discovered, not hardcoded: ``codex debug models`` prints the
catalog. It silently falls back to a cached or bundled copy when it cannot
fetch, so the driver deletes the cache first and accepts the list only if the
CLI wrote a new cache during the run (a fetch from OpenAI really happened).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from functools import partial
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
from modelmux.drivers.codex import parser
from modelmux.drivers.events import (
    NormalizedEvent,
    sanitize_detail,
)
from modelmux.errors import ModelMuxError, ProviderAuthError, ProviderError

SYSTEM_PROMPT_FILE = "system-prompt.txt"
MODEL_CACHE = Path(".codex") / "models_cache.json"  # under the driver home
MAX_MODEL_CACHE_BYTES = 32 * 1024 * 1024
# fetched_at comes from the same clock; allow for rounding only.
CLOCK_SLACK = timedelta(seconds=5)
DISCOVERY_BUDGET = 60.0

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

    # ---------------------------------------------------------------- invocation

    def lockdown_args(self, cli_model: str | None, workspace: Path) -> tuple[str, ...]:
        """Every flag of a real request. ``cli_model`` None means the CLI's default."""
        model = ["--model", cli_model] if cli_model is not None else []
        args = [
            "exec",
            "--json",
            *model,
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

    def _invocation(
        self, cli_model: str | None, workspace: Path, system: str, stdin: bytes
    ) -> Invocation:
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

    def lockdown_spec(self) -> tuple[str, ...]:
        return (
            *self.lockdown_args("<model>", Path("<workspace>")),
            *(f"verified-key={key}" for key in VERIFIED_KEYS),
            "discovery=debug models",
        )

    # ---------------------------------------------------------------- certify (build time)

    async def certify(self, ctx: ProbeContext) -> CertifyResult:
        """Version, every flag, feature and config key, and the catalog format.

        The models depend on the account (the live catalog differs from the
        bundled one), so they are discovered at startup instead.
        """
        version = None
        try:
            version = await self.check_version(ctx)
            await self._check_flags(ctx)
            await self._check_config_keys(ctx)
            await self._check_catalog_format(ctx)
        except CheckError as exc:
            return CertifyResult(ok=False, reason=str(exc), version=version)
        except ModelMuxError as exc:
            return CertifyResult(ok=False, reason=f"run failed: {exc.code}", version=version)
        return CertifyResult(ok=True, reason="ok", version=version, models=None)

    async def _check_flags(self, ctx: ProbeContext) -> None:
        """Every flag and feature name must be accepted. Empty stdin: no API call."""
        out = await ctx.run(
            lambda ws: self._invocation(None, ws, PROBE_SYSTEM, b""),
            budget=PROBE_BUDGET,
        )
        text = out.text()
        if unknown := _UNKNOWN_FLAG_RE.search(text):
            flag = sanitize_detail(unknown.group(1), 64)
            raise CheckError(f"CLI does not support lockdown flag {flag}")
        if unknown := _UNKNOWN_FEATURE_RE.search(text):
            feature = sanitize_detail(unknown.group(1), 64)
            raise CheckError(f"CLI does not know feature {feature}")
        if CONFIG_ERROR in text:
            raise CheckError("CLI rejected the lockdown configuration")
        if NO_PROMPT not in text:
            raise CheckError("lockdown flag check gave an unexpected response")

    async def _check_config_keys(self, ctx: ProbeContext) -> None:
        """Codex ignores unknown -c keys, so prove each one is recognised."""
        for key in VERIFIED_KEYS:
            invocation = Invocation(
                argv=self.argv("exec", "--json", "--ignore-user-config", "-c", f"{key}=[1]", "-"),
                stdin=b"",
            )
            out = await ctx.run(partial(_fixed, invocation), budget=PROBE_BUDGET)
            text = out.text()
            if CONFIG_ERROR not in text or f"`{key}`" not in text:
                raise CheckError(f"CLI does not recognise config key {key}")

    async def _check_catalog_format(self, ctx: ProbeContext) -> None:
        """``debug models`` exists and prints a catalog with usable models (offline)."""
        invocation = Invocation(argv=self.argv("debug", "models", "--bundled"), stdin=b"")
        out = await ctx.run(partial(_fixed, invocation), budget=DISCOVERY_BUDGET)
        catalog = parser.parse_model_catalog(b"\n".join(out.lines))
        if out.exit_code != 0 or catalog is None or not any(m.visible for m in catalog):
            raise CheckError("CLI does not print a usable model catalog")

    # ---------------------------------------------------------------- probe (startup)

    async def probe(self, ctx: ProbeContext, certified: Certified) -> ProbeResult:
        """Discover the account's current models, then make one live request."""
        try:
            models = await self._discover_models(ctx, certified.version)
            await self.check_live(
                ctx,
                lambda ws: self.build_invocation(
                    DriverRequest(models[0].cli_model, PROBE_SYSTEM, PROBE_PROMPT, ws, "probe")
                ),
                budget=LIVE_PROBE_BUDGET,
            )
        except CheckError as exc:
            return ProbeResult(ok=False, reason=str(exc))
        except ModelMuxError as exc:
            return ProbeResult(ok=False, reason=f"probe run failed: {exc.code}")
        return ProbeResult(ok=True, reason="ok", version=certified.version, models=models)

    async def _discover_models(self, ctx: ProbeContext, version: str) -> tuple[ModelInfo, ...]:
        """The visible models of a catalog fetched from OpenAI during this run,
        highest priority first. Codex silently falls back to a cached or
        bundled copy, so the cache is deleted first and must be rewritten."""
        cache = ctx.home / MODEL_CACHE
        try:
            cache.unlink(missing_ok=True)
        except OSError:
            raise CheckError("could not reset the Codex model cache") from None
        started = datetime.now(UTC)
        invocation = Invocation(argv=self.argv("debug", "models"), stdin=b"")
        out = await ctx.run(partial(_fixed, invocation), budget=DISCOVERY_BUDGET)
        if out.exit_code != 0:
            raise CheckError(f"model list command failed (exit {out.exit_code})")
        catalog = parser.parse_model_catalog(b"\n".join(out.lines))
        if catalog is None:
            raise CheckError("could not read the model list")
        fetched = _read_model_cache(cache)
        if fetched is None:
            raise CheckError("the model list was not fetched from OpenAI (not logged in?)")
        fresh = (
            fetched.fetched_at >= started - CLOCK_SLACK
            and fetched.client_version == version
            and fetched.slugs == {model.slug for model in catalog}
        )
        if not fresh:
            raise CheckError("the model list is not a fresh copy from OpenAI")
        visible = [model.slug for model in catalog if model.visible]
        if not visible:
            raise CheckError("the model list has no usable models")
        return tuple(ModelInfo(id=slug, cli_model=slug) for slug in visible)

    def integrity_files(self) -> tuple[Path, ...]:
        """``codex`` is a Node launcher for a native binary in a per-platform
        package next to it; both are bound to the certification."""
        parents = self.binary.resolve().parents
        if len(parents) < 3:
            return (self.binary,)
        native = sorted(parents[2].glob("codex-*/vendor/*/bin/codex"))  # node_modules/@openai
        return (self.binary, *native)


def _read_model_cache(path: Path) -> parser.CatalogCache | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_MODEL_CACHE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    return parser.parse_model_cache(raw)
