from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux.drivers.base import Certified, DriverRequest, ModelInfo, ProbeContext
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


# ------------------------------------------------------------------ certify (build time)

DISCOVERED = [
    "sonnet", "opus", "haiku", "fable",
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5-20251001", "claude-fable-5-1",
]  # fmt: skip


async def test_certify_ok(driver: ClaudeDriver, work_root: Path) -> None:
    # First alias per full ID; [1m] variants and duplicates (best, opusplan,
    # default) drop out without any list of names to skip.
    result = await driver.certify(probe_ctx(driver, work_root))
    assert result.ok, result.reason
    assert result.version == "2.1.285"
    assert result.models is not None
    assert [m.id for m in result.models] == DISCOVERED
    assert all(m.id == m.cli_model for m in result.models)


async def test_certify_follows_the_cli(driver: ClaudeDriver, work_root: Path) -> None:
    # A new alias appears without any code change; an alias that is already a
    # full ID ("nova" resolves to itself) is published once.
    ctx = probe_ctx(driver, work_root, FAKE_MODEL_LIST="sonnet, haiku, nova, default")
    result = await driver.certify(ctx)
    assert result.models is not None
    assert [m.id for m in result.models] == [
        "sonnet", "haiku", "nova", "claude-sonnet-5-5", "claude-haiku-4-5-20251001",
    ]  # fmt: skip


async def test_certify_model_query_argv(
    driver: ClaudeDriver, work_root: Path, tmp_path: Path
) -> None:
    log = tmp_path / "queries.jsonl"
    assert (await driver.certify(probe_ctx(driver, work_root, FAKE_MODEL_LOG=str(log)))).ok
    queries = [json.loads(line) for line in log.read_text().splitlines()]
    # One listing query, then one per alias without a [ suffix.
    assert len(queries) == 1 + 7
    assert "--model" not in queries[0]
    resolved = sorted(q[q.index("--model") + 1] for q in queries[1:])
    assert resolved == sorted(["sonnet", "opus", "haiku", "fable", "best", "opusplan", "default"])
    for argv in queries:
        # Every lockdown flag except the one that would hide /model.
        assert "--disable-slash-commands" not in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--max-turns") + 1] == "1"
        for flag in ("--strict-mcp-config", "--safe-mode", "--restricted",
                     "--no-session-persistence"):  # fmt: skip
            assert flag in argv


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"FAKE_MODEL_QUERY": "real"}, "model query made a real model request"),
        ({"FAKE_MODEL_QUERY": "tools"}, "model query triggered the sandbox tripwire"),
        ({"FAKE_MODEL_QUERY": "user_command"}, "model query triggered the sandbox tripwire"),
        ({"FAKE_MODEL_LIST": "sonnet; opus"}, "could not read the model list"),
        ({"FAKE_MODEL_LIST": "--evil, sonnet"}, "could not read the model list"),
        ({"FAKE_UNKNOWN_FLAG": "--safe-mode"}, "CLI does not support lockdown flag --safe-mode"),
        ({"FAKE_VERSION": "1.9.0 (Claude Code)"}, "CLI version 1.9.0 is not in >=2.1,<3"),
        ({"FAKE_VERSION": "garbage"}, "could not read the CLI version"),
    ],
)
async def test_certify_fails_closed(
    driver: ClaudeDriver, work_root: Path, env: dict[str, str], reason: str
) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, **env))
    assert not result.ok
    assert result.reason == reason
    assert result.models is None


@pytest.mark.parametrize("flag", ["--max-turns", "--system-prompt-file", "--tools"])
async def test_certify_missing_flag(driver: ClaudeDriver, work_root: Path, flag: str) -> None:
    result = await driver.certify(probe_ctx(driver, work_root, FAKE_UNKNOWN_FLAG=flag))
    assert result.reason == f"CLI does not support lockdown flag {flag}"
    assert result.version == "2.1.285"


async def test_certify_unexpected_flag_response(
    driver: ClaudeDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CLI that does not answer empty input the way we verified is refused.
    monkeypatch.setattr(claude_module, "INPUT_REQUIRED", "something the fake never prints")
    result = await driver.certify(probe_ctx(driver, work_root))
    assert result.reason == "lockdown flag check gave an unexpected response"


async def test_certify_run_failure(
    driver: ClaudeDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_module, "PROBE_BUDGET", 0.3)
    ctx = probe_ctx(driver, work_root, FAKE_SCENARIO="hang_before_output")
    monkeypatch.setattr(claude_module, "MODEL_QUERY", b"hang\n")  # becomes a normal request
    result = await driver.certify(ctx)
    assert result.reason == "run failed: provider_timeout"


# ------------------------------------------------------------------ probe (startup)

CERTIFIED = Certified("2.1.285", tuple(ModelInfo(m, m) for m in DISCOVERED))


async def test_probe_ok(driver: ClaudeDriver, work_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "probe.json"
    result = await driver.probe(probe_ctx(driver, work_root, FAKE_PROBE_OUT=str(out)), CERTIFIED)
    assert result.ok, result.reason
    assert result.models == CERTIFIED.models
    assert result.version == "2.1.285"
    # One request, the normal locked-down argv, on the account's default model.
    argv = json.loads(out.read_text())
    assert "--model" not in argv
    assert "--disable-slash-commands" in argv


async def test_probe_runs_one_process(
    driver: ClaudeDriver, work_root: Path, tmp_path: Path
) -> None:
    log = tmp_path / "queries.jsonl"
    ctx = probe_ctx(driver, work_root, FAKE_MODEL_LOG=str(log))
    assert (await driver.probe(ctx, CERTIFIED)).ok
    assert not log.exists()  # no /model query at startup


async def test_probe_needs_certified_models(driver: ClaudeDriver, work_root: Path) -> None:
    result = await driver.probe(probe_ctx(driver, work_root), Certified("2.1.285", None))
    assert result.reason == "the manifest lists no Claude models"


@pytest.mark.parametrize(
    ("fixture", "exit_code", "reason"),
    [("auth_error.jsonl", "1", "live check failed: auth"),
     ("init_with_tools.jsonl", "0", "live check triggered the sandbox tripwire"),
     ("no_completion.jsonl", "0", "live check did not complete")],
)  # fmt: skip
async def test_probe_live_failures(
    driver: ClaudeDriver, work_root: Path, fixture: str, exit_code: str, reason: str
) -> None:
    ctx = probe_ctx(
        driver, work_root, FAKE_PROBE="claude_fixture",
        FAKE_FIXTURE=str(FIXTURES / "claude" / fixture), FAKE_EXIT=exit_code,
    )  # fmt: skip
    result = await driver.probe(ctx, CERTIFIED)
    assert not result.ok
    assert result.reason == reason


async def test_probe_timeout(
    driver: ClaudeDriver, work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_module, "LIVE_PROBE_BUDGET", 0.5)
    ctx = probe_ctx(driver, work_root, FAKE_PROBE="hang_before_output")
    result = await driver.probe(ctx, CERTIFIED)
    assert result.reason == "probe run failed: provider_timeout"


def test_workspace_file_written_private(driver: ClaudeDriver, work_root: Path) -> None:
    with create_workspace(work_root) as ws:
        req = DriverRequest("sonnet", "sys", "t", ws.path, "r")
        inv = driver.build_invocation(req)
        for name, data in inv.files.items():
            path = ws.write_file(name, data)
            assert oct(path.stat().st_mode & 0o777) == "0o600"
