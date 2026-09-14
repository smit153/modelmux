from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from modelmux.errors import InternalError, OutputTooLargeError, ProviderTimeoutError
from modelmux.runtime.env import CORE_KEYS
from modelmux.runtime.runner import (
    BinaryResolutionError,
    Invocation,
    Run,
    RunLimits,
    Runner,
    resolve_binary,
)
from modelmux.runtime.workspace import Workspace, create_workspace, prepare_work_root
from tests.fakes import FAKE_ENV_KEYS, install_fake_cli
from tests.proc import alive, group_members, wait_dead

FAST = RunLimits(
    first_output_timeout=5.0,
    idle_timeout=5.0,
    total_timeout=10.0,
    kill_grace=0.3,
    max_line_bytes=64 * 1024,
    max_stdout_bytes=1024 * 1024,
    max_stderr_bytes=1024,
)


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    return resolve_binary("fake-cli", install_fake_cli(tmp_path / "bin"))


@pytest.fixture
def work_root(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    prepare_work_root(root)
    return root


@pytest.fixture
def workspace(work_root: Path) -> Iterator[Workspace]:
    with create_workspace(work_root) as ws:
        yield ws


def make_runner(binary: Path, limits: RunLimits = FAST, **env: str) -> Runner:
    return Runner(
        binary,
        home=Path("/nonexistent-home"),
        env_allowlist=FAKE_ENV_KEYS,
        limits=limits,
        source_env=env,
    )


def inv(binary: Path, stdin: bytes = b"", *args: str, **files: bytes) -> Invocation:
    return Invocation(argv=(str(binary), *args), stdin=stdin, files=files)


async def collect(run: Run) -> list[bytes]:
    return [line async for line in run]


def assert_group_gone(run: Run) -> None:
    assert run.pid is not None
    assert not alive(run.pid)
    assert group_members(run.pid) == []


# ------------------------------------------------------------------ happy paths


async def test_echo(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="echo")
    async with runner.start(inv(binary, b"hello\nworld\n"), workspace) as run:
        lines = await collect(run)
    assert lines == [b"hello", b"world"]
    assert run.result.exit_code == 0
    assert run.result.stdout_bytes == 12
    assert run.result.duration > 0
    assert_group_gone(run)


async def test_environment_argv_cwd_and_files(
    binary: Path, workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "dump.json"
    # Use the real os.environ as the source, polluted with secrets.
    monkeypatch.setenv("MODELMUX_API_KEYS", "k" * 40)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("FAKE_SCENARIO", "dump")
    monkeypatch.setenv("FAKE_OUT", str(out))
    runner = Runner(binary, home=Path("/driver-home"), env_allowlist=FAKE_ENV_KEYS, limits=FAST)
    invocation = Invocation(
        argv=(str(binary), "-p", "--model", "sonnet"),
        stdin=b"prompt-bytes",
        files={"system-prompt.txt": b"be nice"},
    )
    async with runner.start(invocation, workspace) as run:
        assert await collect(run) == [b"dumped"]

    data = json.loads(out.read_text())
    assert set(data["env"]) <= CORE_KEYS | FAKE_ENV_KEYS
    assert data["env"]["HOME"] == "/driver-home"
    assert data["env"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "MODELMUX_API_KEYS" not in data["env"]
    assert "aws-secret" not in out.read_text()
    assert data["argv"][1:] == ["-p", "--model", "sonnet"]
    assert Path(data["cwd"]).resolve() == workspace.path.resolve()
    assert data["stdin_bytes"] == len(b"prompt-bytes")
    assert data["files"] == {"system-prompt.txt": {"mode": "0o600", "text": "be nice"}}
    assert data["sid"] == data["pid"] == data["pgid"]  # own session and group


async def test_no_trailing_newline(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="no_trailing_newline")
    async with runner.start(inv(binary), workspace) as run:
        assert await collect(run) == [b"first", b"last-without-newline"]


async def test_crlf_stripped(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="crlf")
    async with runner.start(inv(binary), workspace) as run:
        assert await collect(run) == [b"one", b"two"]


async def test_nonzero_exit_and_stderr_tail(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="exit_code")
    async with runner.start(inv(binary), workspace) as run:
        assert await collect(run) == [b"partial"]
    assert run.result.exit_code == 3
    assert "STDERR_SENTINEL" in run.result.stderr_tail


async def test_stderr_ring_buffer(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="stderr_flood", FAKE_SIZE=str(1024 * 1024))
    async with runner.start(inv(binary), workspace) as run:
        assert await collect(run) == [b"done"]
    tail = run.result.stderr_tail
    assert len(tail) <= FAST.max_stderr_bytes
    assert tail.rstrip().endswith("STDERR_TAIL_MARKER")


async def test_large_stdin_ignored_by_cli(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="ignore_stdin")
    async with runner.start(inv(binary, b"x" * (4 * 1024 * 1024)), workspace) as run:
        assert await collect(run) == [b"ignored stdin"]
    assert run.result.exit_code == 0


async def test_large_stdin_consumed(binary: Path, workspace: Workspace) -> None:
    payload = b"".join(b"%d\n" % i for i in range(100_000))
    runner = make_runner(binary, FAKE_SCENARIO="echo")
    async with runner.start(inv(binary, payload), workspace) as run:
        lines = await collect(run)
    assert len(lines) == 100_000


# ------------------------------------------------------------------ timeouts


async def expect_timeout(runner: Runner, binary: Path, ws: Workspace, which: str) -> Run:
    started = time.monotonic()
    async with runner.start(inv(binary), ws) as run:
        with pytest.raises(ProviderTimeoutError) as info:
            await collect(run)
    assert which in str(info.value)
    assert time.monotonic() - started < 3
    assert_group_gone(run)
    return run


async def test_first_output_timeout(binary: Path, workspace: Workspace) -> None:
    limits = replace(FAST, first_output_timeout=0.3)
    runner = make_runner(binary, limits, FAKE_SCENARIO="hang_before_output")
    await expect_timeout(runner, binary, workspace, "first_output")


async def test_partial_line_does_not_count_as_output(binary: Path, workspace: Workspace) -> None:
    limits = replace(FAST, first_output_timeout=0.4)
    runner = make_runner(binary, limits, FAKE_SCENARIO="partial_line_trickle")
    await expect_timeout(runner, binary, workspace, "first_output")


async def test_idle_timeout(binary: Path, workspace: Workspace) -> None:
    limits = replace(FAST, idle_timeout=0.3)
    runner = make_runner(binary, limits, FAKE_SCENARIO="hang_mid_stream")
    await expect_timeout(runner, binary, workspace, "idle")


async def test_total_timeout(binary: Path, workspace: Workspace) -> None:
    limits = replace(FAST, idle_timeout=0.4, first_output_timeout=0.4, total_timeout=0.6)
    runner = make_runner(binary, limits, FAKE_SCENARIO="slow_forever")
    await expect_timeout(runner, binary, workspace, "total")


async def test_external_deadline(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="slow_forever")
    deadline = asyncio.get_running_loop().time() + 0.4
    async with runner.start(inv(binary), workspace, deadline=deadline) as run:
        with pytest.raises(ProviderTimeoutError, match="total"):
            await collect(run)
    assert_group_gone(run)


async def test_slow_consumer_is_not_idle(binary: Path, workspace: Workspace) -> None:
    limits = replace(FAST, idle_timeout=0.2)
    runner = make_runner(binary, limits, FAKE_SCENARIO="lines", FAKE_SIZE="3")
    lines = []
    async with runner.start(inv(binary), workspace) as run:
        async for line in run:
            lines.append(line)
            await asyncio.sleep(0.3)  # slower than idle_timeout
    assert len(lines) == 3


# ------------------------------------------------------------------ caps


async def test_line_cap(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="long_line", FAKE_SIZE=str(200 * 1024))
    async with runner.start(inv(binary), workspace) as run:
        with pytest.raises(OutputTooLargeError, match="line"):
            await collect(run)
    assert_group_gone(run)


async def test_stdout_total_cap(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="big_output", FAKE_SIZE=str(2 * 1024 * 1024))
    async with runner.start(inv(binary), workspace) as run:
        with pytest.raises(OutputTooLargeError, match="total"):
            await collect(run)
    assert_group_gone(run)


# ------------------------------------------------------------------ killing


async def test_group_kill_escalates_to_sigkill(
    binary: Path, workspace: Workspace, tmp_path: Path
) -> None:
    out = tmp_path / "pids.json"
    runner = make_runner(binary, FAKE_SCENARIO="ignore_term_child", FAKE_OUT=str(out))
    started = time.monotonic()
    async with runner.start(inv(binary), workspace) as run:
        async for _line in run:
            break  # e.g. the tripwire fired on the first event
    elapsed = time.monotonic() - started
    pids = json.loads(out.read_text())
    assert wait_dead(pids["child_pid"], 1.0)
    assert not alive(pids["pid"])
    assert group_members(pids["pid"]) == []
    assert elapsed < FAST.kill_grace + 1.0


async def test_children_killed_after_clean_exit(
    binary: Path, workspace: Workspace, tmp_path: Path
) -> None:
    out = tmp_path / "pids.json"
    runner = make_runner(binary, FAKE_SCENARIO="orphan_child_then_exit", FAKE_OUT=str(out))
    async with runner.start(inv(binary), workspace) as run:
        assert await collect(run) == [b"bye"]
    pids = json.loads(out.read_text())
    assert wait_dead(pids["child_pid"], 1.0)
    assert group_members(pids["pid"]) == []


async def test_cancellation_kills(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="hang_before_output")
    holder: list[Run] = []
    spawned = asyncio.Event()

    async def consume() -> None:
        async with runner.start(inv(binary), workspace) as run:
            holder.append(run)
            spawned.set()
            await collect(run)

    task = asyncio.create_task(consume())
    await spawned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_group_gone(holder[0])


async def test_kill_is_idempotent_and_concurrent(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="hang_before_output")
    async with runner.start(inv(binary), workspace) as run:
        await asyncio.gather(run.kill("a"), run.kill("b"), run.kill("c"))
        await run.kill("again")
    assert_group_gone(run)


# ------------------------------------------------------------------ guards


async def test_argv0_must_be_binary(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary)
    with pytest.raises(InternalError):
        runner.start(Invocation(argv=("/bin/sh", "-c", "id"), stdin=b""), workspace)
    with pytest.raises(InternalError):
        runner.start(Invocation(argv=(), stdin=b""), workspace)


async def test_nul_in_argv_rejected(binary: Path, workspace: Workspace) -> None:
    runner = make_runner(binary)
    with pytest.raises(InternalError):
        runner.start(inv(binary, b"", "a\x00b"), workspace)


async def test_result_before_finish_and_double_iteration(
    binary: Path, workspace: Workspace
) -> None:
    runner = make_runner(binary, FAKE_SCENARIO="echo")
    async with runner.start(inv(binary, b"a\n"), workspace) as run:
        with pytest.raises(InternalError):
            _ = run.result
        await collect(run)
        with pytest.raises(InternalError):
            run.__aiter__()


def test_relative_binary_rejected() -> None:
    with pytest.raises(InternalError):
        Runner(Path("fake"), home=Path("/h"), env_allowlist=frozenset(), limits=FAST)


# ------------------------------------------------------------------ binary resolution


def test_resolve_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = install_fake_cli(tmp_path / "bin", "mycli")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert resolve_binary("mycli") == wrapper.resolve()


def test_resolve_follows_symlink(tmp_path: Path) -> None:
    wrapper = install_fake_cli(tmp_path / "bin")
    link = tmp_path / "link"
    link.symlink_to(wrapper)
    assert resolve_binary("x", link) == wrapper.resolve()


def test_resolve_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(BinaryResolutionError, match="not found"):
        resolve_binary("definitely-not-here")
    with pytest.raises(BinaryResolutionError, match="does not exist"):
        resolve_binary("x", tmp_path / "missing")


def test_resolve_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(BinaryResolutionError, match="regular file"):
        resolve_binary("x", tmp_path)


def test_resolve_rejects_non_executable(tmp_path: Path) -> None:
    f = tmp_path / "cli"
    f.write_text("x")
    os.chmod(f, 0o600)
    with pytest.raises(BinaryResolutionError, match="not executable"):
        resolve_binary("x", f)


def test_resolve_rejects_world_writable(tmp_path: Path) -> None:
    wrapper = install_fake_cli(tmp_path / "bin")
    os.chmod(wrapper, 0o702)  # noqa: S103 - deliberately unsafe
    with pytest.raises(BinaryResolutionError, match="world-writable"):
        resolve_binary("x", wrapper)


def test_resolve_rejects_world_writable_dir(tmp_path: Path) -> None:
    wrapper = install_fake_cli(tmp_path / "open")
    os.chmod(tmp_path / "open", 0o777)  # noqa: S103 - deliberately unsafe
    with pytest.raises(BinaryResolutionError, match="directory"):
        resolve_binary("x", wrapper)
    os.chmod(tmp_path / "open", 0o777 | stat.S_ISVTX)  # noqa: S103 - sticky like /tmp is ok
    assert resolve_binary("x", wrapper) == wrapper.resolve()
