from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux.drivers.base import DriverRequest, ProbeContext
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
    runner = Runner(
        driver.binary,
        home=Path("/nonexistent"),
        env_allowlist=driver.env_allowlist() | FAKE_ENV_KEYS,
        limits=RunLimits(first_output_timeout=5, idle_timeout=5, total_timeout=10, kill_grace=0.2),
        source_env=env,
    )
    return ProbeContext(runner, work_root)


def test_registered_builtin(driver: CodexDriver) -> None:
    cls = load_driver_class("codex")
    assert cls is CodexDriver
    create_driver(cls, driver.binary)


def test_default_models(driver: CodexDriver) -> None:
    assert [m.id for m in driver.models()] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"]


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


async def test_probe_ok(driver: CodexDriver, work_root: Path) -> None:
    result = await driver.probe(probe_ctx(driver, work_root))
    assert result.ok, result.reason
    assert result.version == "0.159.2"


@pytest.mark.parametrize("flag", ["--ephemeral", "--ignore-rules", "--sandbox"])
async def test_probe_missing_flag(driver: CodexDriver, work_root: Path, flag: str) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_UNKNOWN_FLAG=flag))
    assert result.reason == f"CLI does not support lockdown flag {flag}"


async def test_probe_unknown_feature(driver: CodexDriver, work_root: Path) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_UNKNOWN_FEATURE="shell_tool"))
    assert result.reason == "CLI does not know feature shell_tool"


@pytest.mark.parametrize("key", VERIFIED_KEYS)
async def test_probe_unrecognised_config_key(
    driver: CodexDriver, work_root: Path, key: str
) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_UNKNOWN_KEY=key))
    assert result.reason == f"CLI does not recognise config key {key}"


async def test_probe_version(driver: CodexDriver, work_root: Path) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_VERSION="codex-cli 0.100.0"))
    assert result.reason == "CLI version 0.100.0 is not in >=0.159,<1.0"


async def test_probe_live_auth_failure(driver: CodexDriver, work_root: Path) -> None:
    fixture = str(FIXTURES / "codex" / "auth_error.jsonl")
    ctx = probe_ctx(
        driver, work_root, FAKE_PROBE="codex_fixture", FAKE_FIXTURE=fixture, FAKE_EXIT="1"
    )
    result = await driver.probe(ctx)
    assert result.reason == "live check failed: auth"
    assert result.version is not None


async def test_probe_live_tripwire(driver: CodexDriver, work_root: Path) -> None:
    fixture = str(FIXTURES / "codex" / "command_execution.jsonl")
    ctx = probe_ctx(driver, work_root, FAKE_PROBE="codex_fixture", FAKE_FIXTURE=fixture)
    assert "tripwire" in (await driver.probe(ctx)).reason


async def test_probe_timeout(
    driver: CodexDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_module, "LIVE_PROBE_BUDGET", 0.5)
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_PROBE="hang_before_output"))
    assert result.reason == "probe run failed: provider_timeout"
