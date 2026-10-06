from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux.drivers.base import Certified, DriverRequest, ProbeContext
from modelmux.drivers.codex import driver as codex_module
from modelmux.drivers.codex.driver import (
    CONFIG_OVERRIDES,
    DISABLED_FEATURES,
    VERIFIED_KEYS,
    CodexDriver,
)
from modelmux.drivers.registry import create_driver, load_driver_class
from modelmux.errors import ProviderAuthError, ProviderError
from modelmux.runtime.runner import RunLimits, Runner, resolve_binary
from modelmux.runtime.workspace import prepare_work_root
from tests.fakes import FAKE_ENV_KEYS, FIXTURES, install_fake_cli


@pytest.fixture
def driver(tmp_path: Path) -> CodexDriver:
    return CodexDriver(resolve_binary("codex", install_fake_cli(tmp_path / "bin", "codex")))


@pytest.fixture
def work_root(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    prepare_work_root(root)
    return root


def probe_ctx(driver: CodexDriver, work_root: Path, **env: str) -> ProbeContext:
    home = work_root.parent / "home"
    home.mkdir(exist_ok=True)
    runner = Runner(
        driver.binary,
        home=home,
        env_allowlist=driver.env_allowlist() | FAKE_ENV_KEYS,
        limits=RunLimits(first_output_timeout=5, idle_timeout=5, total_timeout=10, kill_grace=0.2),
        source_env=env,
    )
    return ProbeContext(runner, work_root)


def test_registered_builtin(driver: CodexDriver) -> None:
    cls = load_driver_class("codex")
    assert cls is CodexDriver
    create_driver(cls, driver.binary)


def test_build_invocation(driver: CodexDriver, tmp_path: Path) -> None:
    req = DriverRequest("gpt-6.1-sol", "SYSTEM-SENTINEL", "TRANSCRIPT-SENTINEL", tmp_path, "r")
    inv = driver.build_invocation(req)
    argv = list(inv.argv)
    assert argv[0] == str(driver.binary)
    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[argv.index("--color") + 1] == "never"
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
        assert flag in argv
    assert argv[-1] == "-"
    configs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    for key, value in CONFIG_OVERRIDES:
        assert f"{key}={value}" in configs
    instructions = tmp_path / "system-prompt.txt"
    assert f"model_instructions_file={json.dumps(str(instructions))}" in configs
    disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
    assert disabled == list(DISABLED_FEATURES)
    for dangerous in ("--dangerously-bypass-approvals-and-sandbox", "--approve-for-me",
                      "danger-full-access", "workspace-write", "--add-dir"):  # fmt: skip
        assert dangerous not in argv
    assert not any("SENTINEL" in a for a in argv)
    assert inv.stdin == b"TRANSCRIPT-SENTINEL"
    assert inv.files == {"system-prompt.txt": b"SYSTEM-SENTINEL"}
    assert dict(inv.env) == {}


def test_classify_exit(driver: CodexDriver) -> None:
    assert isinstance(driver.classify_exit(2, "error: unexpected argument '--x' found", []),
                      ProviderError)  # fmt: skip
    assert isinstance(driver.classify_exit(1, "Error loading config.toml: x", []), ProviderError)
    assert isinstance(driver.classify_exit(1, "401 Unauthorized", []), ProviderAuthError)
    assert driver.classify_exit(1, "boom", []) is None


# ------------------------------------------------------------------ certify (build time)


async def test_certify_ok(driver: CodexDriver, work_root: Path, tmp_path: Path) -> None:
    result = await driver.certify(probe_ctx(driver, work_root))
    assert result.ok, result.reason
    assert result.version == "0.159.2"
    assert result.models is None  # the account decides: discovered at startup


@pytest.mark.parametrize("flag", ["--ephemeral", "--ignore-rules", "--sandbox"])
async def test_certify_missing_flag(driver: CodexDriver, work_root: Path, flag: str) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, FAKE_UNKNOWN_FLAG=flag))
    assert result.reason == f"CLI does not support lockdown flag {flag}"
    assert result.version == "0.159.2"


async def test_certify_unknown_feature(driver: CodexDriver, work_root: Path) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, FAKE_UNKNOWN_FEATURE="shell_tool"))
    assert result.reason == "CLI does not know feature shell_tool"


@pytest.mark.parametrize("key", VERIFIED_KEYS)
async def test_certify_unrecognised_config_key(
    driver: CodexDriver, work_root: Path, key: str
) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, FAKE_UNKNOWN_KEY=key))
    assert result.reason == f"CLI does not recognise config key {key}"


async def test_certify_version(driver: CodexDriver, work_root: Path) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, FAKE_VERSION="codex-cli 0.100.0"))
    assert result.reason == "CLI version 0.100.0 is not in >=0.159,<1.0"


@pytest.mark.parametrize("catalog", ["garbage", "fail"])
async def test_certify_catalog_format(driver: CodexDriver, work_root: Path, catalog: str) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, FAKE_CATALOG=catalog))
    assert result.reason == "CLI does not print a usable model catalog"


# ------------------------------------------------------------------ probe (startup)

CERTIFIED = Certified("0.159.2", None)

# Visible models of the recorded 0.159.2 catalog, by priority (hidden ones dropped).
VISIBLE = [
    "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
]  # fmt: skip


async def test_probe_ok(driver: CodexDriver, work_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "probe.json"
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_PROBE_OUT=str(out)), CERTIFIED)
    assert result.ok, result.reason
    assert result.version == "0.159.2"
    assert [m.id for m in result.models] == VISIBLE
    assert all(m.id == m.cli_model for m in result.models)
    argv = json.loads(out.read_text())  # the live check uses the top model
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"


async def test_probe_replaces_an_old_cache(driver: CodexDriver, work_root: Path) -> None:
    # A cache left from an earlier start must not count as a fresh fetch.
    ctx = probe_ctx(driver, work_root, FAKE_CATALOG="bundled")
    cache = ctx.home / ".codex" / "models_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"fetched_at": "2099-01-01T00:00:00Z",
                                 "client_version": "0.159.2", "models": []}))  # fmt: skip
    result = await driver.probe(ctx, CERTIFIED)
    assert result.reason == "the model list was not fetched from OpenAI (not logged in?)"
    assert not cache.exists()


@pytest.mark.parametrize(
    ("catalog", "reason"),
    [
        ("bundled", "the model list was not fetched from OpenAI (not logged in?)"),
        ("stale", "the model list is not a fresh copy from OpenAI"),
        ("other_version", "the model list is not a fresh copy from OpenAI"),
        ("mismatch", "the model list is not a fresh copy from OpenAI"),
        ("fail", "model list command failed (exit 1)"),
        ("garbage", "could not read the model list"),
    ],
)
async def test_probe_discovery_fails_closed(
    driver: CodexDriver, work_root: Path, catalog: str, reason: str
) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_CATALOG=catalog), CERTIFIED)
    assert not result.ok
    assert result.reason == reason
    assert result.models == ()


async def test_probe_cache_version_must_match_certified(
    driver: CodexDriver, work_root: Path
) -> None:
    result = await driver.probe(probe_ctx(driver, work_root), Certified("0.160.0", None))
    assert result.reason == "the model list is not a fresh copy from OpenAI"


async def test_probe_cannot_reset_cache(driver: CodexDriver, work_root: Path) -> None:
    ctx = probe_ctx(driver, work_root)
    (ctx.home / ".codex" / "models_cache.json").mkdir(parents=True)  # unlink fails
    result = await driver.probe(ctx, CERTIFIED)
    assert result.reason == "could not reset the Codex model cache"


@pytest.mark.parametrize(
    ("fixture", "exit_code", "reason"),
    [("auth_error.jsonl", "1", "live check failed: auth"),
     ("command_execution.jsonl", "0", "live check triggered the sandbox tripwire")],
)  # fmt: skip
async def test_probe_live_failures(
    driver: CodexDriver, work_root: Path, fixture: str, exit_code: str, reason: str
) -> None:
    ctx = probe_ctx(
        driver, work_root, FAKE_PROBE="codex_fixture",
        FAKE_FIXTURE=str(FIXTURES / "codex" / fixture), FAKE_EXIT=exit_code,
    )  # fmt: skip
    result = await driver.probe(ctx, CERTIFIED)
    assert result.reason == reason


async def test_probe_timeout(
    driver: CodexDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_module, "LIVE_PROBE_BUDGET", 0.5)
    ctx = probe_ctx(driver, work_root, FAKE_PROBE="hang_before_output")
    result = await driver.probe(ctx, CERTIFIED)
    assert result.reason == "probe run failed: provider_timeout"


def test_integrity_files_include_the_native_binary(tmp_path: Path) -> None:
    # Real layout: node_modules/@openai/codex/bin/codex.js launches
    # node_modules/@openai/codex-<platform>/vendor/<triple>/bin/codex.
    scope = tmp_path / "node_modules" / "@openai"
    launcher = scope / "codex" / "bin" / "codex.js"
    native = scope / "codex-linux-x64" / "vendor" / "x86_64-unknown-linux-musl" / "bin" / "codex"
    for path in (launcher, native):
        path.parent.mkdir(parents=True)
        path.write_text("x")
    assert CodexDriver(launcher).integrity_files() == (launcher, native)
