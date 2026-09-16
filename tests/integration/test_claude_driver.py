from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux.drivers.base import DriverRequest, ProbeContext
from modelmux.drivers.claude import driver as claude_module
from modelmux.drivers.claude.driver import DISALLOWED_TOOLS, ClaudeDriver
from modelmux.drivers.registry import create_driver, load_driver_class
from modelmux.errors import ProviderAuthError, ProviderError
from modelmux.runtime.runner import RunLimits, Runner, resolve_binary
from modelmux.runtime.workspace import create_workspace, prepare_work_root
from tests.fakes import FAKE_ENV_KEYS, FIXTURES, install_fake_cli


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    return resolve_binary("claude", install_fake_cli(tmp_path / "bin", "claude"))


@pytest.fixture
def driver(binary: Path) -> ClaudeDriver:
    return ClaudeDriver(binary)


@pytest.fixture
def work_root(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    prepare_work_root(root)
    return root


def probe_ctx(driver: ClaudeDriver, work_root: Path, **env: str) -> ProbeContext:
    runner = Runner(
        driver.binary,
        home=Path("/nonexistent"),
        env_allowlist=driver.env_allowlist() | FAKE_ENV_KEYS,
        limits=RunLimits(first_output_timeout=5, idle_timeout=5, total_timeout=10, kill_grace=0.2),
        source_env=env,
    )
    return ProbeContext(runner, work_root)


def test_registered_builtin(binary: Path) -> None:
    cls = load_driver_class("claude")
    assert cls is ClaudeDriver
    create_driver(cls, binary)


def test_default_models(driver: ClaudeDriver) -> None:
    ids = [m.id for m in driver.models()]
    assert ids[:4] == ["sonnet", "opus", "haiku", "fable"]
    assert "claude-sonnet-5-5" in ids


def test_build_invocation(driver: ClaudeDriver, tmp_path: Path) -> None:
    req = DriverRequest(
        cli_model="sonnet",
        system_prompt="SYSTEM-SENTINEL",
        transcript="TRANSCRIPT-SENTINEL",
        workspace=tmp_path,
        request_id="req_1",
    )
    inv = driver.build_invocation(req)
    argv = list(inv.argv)
    assert argv[0] == str(driver.binary)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert set(argv[argv.index("--disallowedTools") + 1].split(",")) == set(DISALLOWED_TOOLS)
    for flag in ("-p", "--strict-mcp-config", "--safe-mode", "--restricted",
                 "--disable-slash-commands", "--no-session-persistence",
                 "--include-partial-messages"):  # fmt: skip
        assert flag in argv
    assert "bypassPermissions" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--system-prompt-file") + 1] == str(tmp_path / "system-prompt.txt")
    # Prompt content only via stdin and the private file, never argv.
    assert not any("SENTINEL" in a for a in argv)
    assert inv.stdin == b"TRANSCRIPT-SENTINEL"
    assert inv.files == {"system-prompt.txt": b"SYSTEM-SENTINEL"}
    assert set(inv.env) <= driver.env_allowlist()
    assert inv.env["DISABLE_AUTOUPDATER"] == "1"


def test_classify_exit(driver: ClaudeDriver) -> None:
    assert isinstance(driver.classify_exit(1, "error: unknown option '--x'", []), ProviderError)
    assert isinstance(driver.classify_exit(1, "Invalid API key · Please run /login", []),
                      ProviderAuthError)  # fmt: skip
    assert driver.classify_exit(1, "segfault", []) is None


async def test_probe_ok(driver: ClaudeDriver, work_root: Path) -> None:
    result = await driver.probe(probe_ctx(driver, work_root))
    assert result.ok, result.reason
    assert result.version == "2.1.285"


async def test_probe_passes_all_lockdown_flags(
    driver: ClaudeDriver, work_root: Path, tmp_path: Path
) -> None:
    # The flag check must use exactly the argv of a real request.
    out = tmp_path / "argv.json"
    ctx = probe_ctx(driver, work_root, FAKE_OUT=str(out), FAKE_SCENARIO="claude_text")
    assert (await driver.probe(ctx)).ok
    assert not out.exists()  # probe requests are not recorded; only real ones are


@pytest.mark.parametrize("flag", ["--max-turns", "--system-prompt-file", "--safe-mode", "--tools"])
async def test_probe_missing_flag(driver: ClaudeDriver, work_root: Path, flag: str) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_UNKNOWN_FLAG=flag))
    assert not result.ok
    assert result.reason == f"CLI does not support lockdown flag {flag}"


@pytest.mark.parametrize(
    ("version", "reason"),
    [("1.9.0 (Claude Code)", "not in"), ("3.0.0 (Claude Code)", "not in"),
     ("garbage", "could not read")],
)  # fmt: skip
async def test_probe_version(
    driver: ClaudeDriver, work_root: Path, version: str, reason: str
) -> None:
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_VERSION=version))
    assert not result.ok
    assert reason in result.reason


async def test_probe_live_auth_failure(driver: ClaudeDriver, work_root: Path) -> None:
    ctx = probe_ctx(
        driver, work_root, FAKE_PROBE="claude_fixture",
        FAKE_FIXTURE=str(FIXTURES / "claude" / "auth_error.jsonl"), FAKE_EXIT="1",
    )  # fmt: skip
    result = await driver.probe(ctx)
    assert not result.ok
    assert result.reason == "live check failed: auth"


async def test_probe_live_tripwire(driver: ClaudeDriver, work_root: Path) -> None:
    ctx = probe_ctx(
        driver, work_root, FAKE_PROBE="claude_fixture",
        FAKE_FIXTURE=str(FIXTURES / "claude" / "init_with_tools.jsonl"),
    )  # fmt: skip
    result = await driver.probe(ctx)
    assert not result.ok
    assert "tripwire" in result.reason


async def test_probe_live_no_completion(driver: ClaudeDriver, work_root: Path) -> None:
    ctx = probe_ctx(
        driver, work_root, FAKE_PROBE="claude_fixture",
        FAKE_FIXTURE=str(FIXTURES / "claude" / "no_completion.jsonl"),
    )  # fmt: skip
    result = await driver.probe(ctx)
    assert result.reason == "live check did not complete"


async def test_probe_unexpected_flag_response(
    driver: ClaudeDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CLI that does not answer empty input the way we verified is refused.
    monkeypatch.setattr(claude_module, "INPUT_REQUIRED", "something the fake never prints")
    result = await driver.probe(probe_ctx(driver, work_root))
    assert result.reason == "lockdown flag check gave an unexpected response"


async def test_probe_timeout(
    driver: ClaudeDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_module, "LIVE_PROBE_BUDGET", 0.5)
    ctx = probe_ctx(driver, work_root, FAKE_PROBE="hang_before_output")
    result = await driver.probe(ctx)
    assert result.reason == "probe run failed: provider_timeout"


def test_workspace_file_written_private(driver: ClaudeDriver, work_root: Path) -> None:
    with create_workspace(work_root) as ws:
        req = DriverRequest("sonnet", "sys", "t", ws.path, "r")
        inv = driver.build_invocation(req)
        for name, data in inv.files.items():
            path = ws.write_file(name, data)
            assert oct(path.stat().st_mode & 0o777) == "0o600"
