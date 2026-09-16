"""The ONLY module that spawns processes.

Every CLI invocation goes through ``Runner.start``. The runner:

- execs an argv list (never a shell) in its own session/process group,
- builds the environment from scratch (``runtime.env``),
- writes the prompt to stdin and closes it,
- yields stdout lines under first-output, idle and total timeouts and
  line/total size caps, while draining stderr into a small ring buffer,
- on any exit path, SIGTERMs the process group, SIGKILLs it after a grace
  period, and reaps the leader.

It does not interpret output; drivers do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import stat
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, NoReturn, Self

from modelmux.errors import (
    InternalError,
    ModelMuxError,
    OutputTooLargeError,
    ProviderTimeoutError,
)
from modelmux.runtime.env import build_env, validate_allowlist
from modelmux.runtime.workspace import Workspace, create_workspace

if TYPE_CHECKING:
    from modelmux.config import Settings

log = logging.getLogger("modelmux.runtime.runner")

READ_CHUNK = 64 * 1024
# After the CLI exits, how long to wait for output still in the pipe before
# treating stdout as closed (a leftover child may hold the pipe open forever).
POST_EXIT_DRAIN = 0.5
# After exit, how long to wait for the stderr drain to finish.
STDERR_SETTLE = 1.0
# How often to check whether the CLI has exited. asyncio's ``Process.wait()``
# only returns once every pipe is closed, which a leftover child can prevent;
# ``returncode`` is set as soon as the leader is reaped.
EXIT_POLL = 0.05
# Upper bound on waiting for pipes to close after SIGKILLing the group. Only
# a descendant that escaped the process group can hit this.
PIPE_CLOSE_TIMEOUT = 5.0


class BinaryResolutionError(Exception):
    """The CLI binary is unusable. The message is safe to print."""


@dataclass(frozen=True)
class Invocation:
    """How to run the CLI once. Built by a driver.

    ``argv[0]`` must be the resolved absolute binary path. ``env`` holds extra
    variables (allowlisted keys only). ``files`` are private files written
    into the workspace (mode 0600) before spawning.
    """

    argv: tuple[str, ...]
    stdin: bytes
    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stderr_tail: str
    duration: float
    stdout_bytes: int


@dataclass(frozen=True)
class RunLimits:
    first_output_timeout: float = 60.0
    idle_timeout: float = 120.0
    total_timeout: float = 600.0
    kill_grace: float = 3.0
    max_line_bytes: int = 4 * 1024 * 1024
    max_stdout_bytes: int = 32 * 1024 * 1024
    max_stderr_bytes: int = 16 * 1024

    @classmethod
    def from_settings(cls, settings: Settings) -> RunLimits:
        return cls(
            first_output_timeout=settings.first_output_timeout,
            idle_timeout=settings.idle_timeout,
            total_timeout=settings.total_timeout,
            kill_grace=settings.kill_grace,
            max_line_bytes=settings.max_line_bytes,
            max_stdout_bytes=settings.max_stdout_bytes,
            max_stderr_bytes=settings.max_stderr_bytes,
        )


def resolve_binary(name: str, cli_path: Path | None = None) -> Path:
    """Resolve the CLI to an absolute, executable, non-world-writable file."""
    if cli_path is not None:
        candidate = str(cli_path)
    else:
        found = shutil.which(name)
        if found is None:
            raise BinaryResolutionError(f"CLI binary {name!r} not found on PATH")
        candidate = found
    try:
        path = Path(candidate).resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise BinaryResolutionError(f"CLI binary for {name!r} does not exist") from None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise BinaryResolutionError(f"CLI binary for {name!r} is not a regular file")
    if not os.access(path, os.X_OK):
        raise BinaryResolutionError(f"CLI binary for {name!r} is not executable")
    if info.st_mode & stat.S_IWOTH:
        raise BinaryResolutionError(f"CLI binary for {name!r} is world-writable")
    parent = path.parent.stat()
    if parent.st_mode & stat.S_IWOTH and not parent.st_mode & stat.S_ISVTX:
        raise BinaryResolutionError(f"CLI binary for {name!r} is in a world-writable directory")
    return path


class Runner:
    """Spawns one driver's CLI. One instance per process, shared by requests."""

    def __init__(
        self,
        binary: Path,
        *,
        home: Path,
        env_allowlist: frozenset[str],
        limits: RunLimits,
        source_env: Mapping[str, str] | None = None,
    ) -> None:
        if not binary.is_absolute():
            raise InternalError("runner binary must be an absolute path")
        self.binary = binary
        self.home = home
        self.env_allowlist = validate_allowlist(env_allowlist)
        self.limits = limits
        self._source_env = source_env

    def start(
        self, invocation: Invocation, workspace: Workspace, *, deadline: float | None = None
    ) -> Run:
        """Prepare a run. Use as ``async with runner.start(...) as run``.

        ``deadline`` is an absolute ``loop.time()`` cap, so a repair attempt
        shares the original request's total budget.
        """
        self._check_invocation(invocation)
        env = build_env(
            home=self.home,
            allowlist=self.env_allowlist,
            driver_env=invocation.env,
            source=self._source_env,
        )
        return Run(invocation, workspace, env, self.limits, deadline)

    def _check_invocation(self, invocation: Invocation) -> None:
        argv = invocation.argv
        if not argv or argv[0] != str(self.binary):
            raise InternalError("argv[0] must be the resolved CLI binary")
        for arg in argv:
            if not isinstance(arg, str) or "\x00" in arg:
                raise InternalError("argv entries must be strings without NUL")


class Run:
    """One CLI process. Iterate it for stdout lines; ``result`` afterwards."""

    def __init__(
        self,
        invocation: Invocation,
        workspace: Workspace,
        env: dict[str, str],
        limits: RunLimits,
        deadline: float | None,
    ) -> None:
        self._invocation = invocation
        self._workspace = workspace
        self._env = env
        self._limits = limits
        self._requested_deadline = deadline
        self._proc: asyncio.subprocess.Process | None = None
        self._stdin_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[int] | None = None
        self._kill_task: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._stderr_total = 0
        self._stdout_total = 0
        self._started = 0.0
        self._deadline = 0.0
        self._kill_reason: str | None = None
        self._result: RunResult | None = None
        self._line_iter: AsyncGenerator[bytes, None] | None = None

    # ---------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Self:
        for name, data in self._invocation.files.items():
            self._workspace.write_file(name, data)
        loop = asyncio.get_running_loop()
        self._started = loop.time()
        total_deadline = self._started + self._limits.total_timeout
        self._deadline = min(total_deadline, self._requested_deadline or total_deadline)

        self._proc = await asyncio.create_subprocess_exec(
            *self._invocation.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace.path,
            env=self._env,
            start_new_session=True,
            close_fds=True,
        )
        log.info(
            "process started",
            extra={
                "event": "process_started",
                "pid": self._proc.pid,
                "argc": len(self._invocation.argv),
                "stdin_bytes": len(self._invocation.stdin),
            },
        )
        self._stdin_task = asyncio.create_task(self._feed_stdin())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._exit_task = asyncio.create_task(self._watch_exit())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None and self._kill_reason is None:
            self._kill_reason = (
                "cancelled" if issubclass(exc_type, asyncio.CancelledError) else "error"
            )
        if self._line_iter is not None:
            with contextlib.suppress(Exception):
                await self._line_iter.aclose()
        await self.kill(self._kill_reason or "finished")
        for task in (self._stdin_task, self._stderr_task, self._exit_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        proc = self._proc
        if self._result is None and proc is not None and proc.returncode is not None:
            self._result = self._make_result(proc.returncode)
        # Close the pipes now instead of at garbage collection, which may happen
        # after the event loop has closed. asyncio has no public API for this.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        log.info(
            "process finished",
            extra={
                "event": "process_finished",
                "pid": proc.pid if proc else None,
                "exit_code": proc.returncode if proc else None,
                "duration_ms": round((self._now() - self._started) * 1000, 1),
                "stdout_bytes": self._stdout_total,
                "stderr_bytes": self._stderr_total,
                "kill_reason": self._kill_reason,
            },
        )

    @property
    def _process(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise InternalError("run used before start")
        return self._proc

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def result(self) -> RunResult:
        if self._result is None:
            raise InternalError("run result requested before the process finished")
        return self._result

    def stderr_tail(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace")

    # ---------------------------------------------------------------- killing

    async def kill(self, reason: str = "killed") -> None:
        """Terminate the whole process group and reap the leader.

        Idempotent and safe to call concurrently. The work runs in a shielded
        task, so cancelling the caller cannot leave the process alive.
        """
        if self._proc is None:
            return
        if self._kill_reason is None:
            self._kill_reason = reason
        if self._kill_task is None:
            self._kill_task = asyncio.ensure_future(self._terminate_group())
        await asyncio.shield(self._kill_task)

    async def _terminate_group(self) -> None:
        proc = self._process
        pgid = proc.pid  # start_new_session: the leader's pid is the group id
        if proc.returncode is None:
            self._signal_group(pgid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._limits.kill_grace):
                    await self._watch_exit()
        # Always SIGKILL the group: it takes out the leader if it ignored
        # SIGTERM and any children left behind even after a clean exit.
        self._signal_group(pgid, signal.SIGKILL)
        try:
            async with asyncio.timeout(PIPE_CLOSE_TIMEOUT):
                await proc.wait()
        except TimeoutError:
            log.error(
                "process pipes still open after group kill",
                extra={"event": "process_escaped", "pid": proc.pid},
            )

    async def _watch_exit(self) -> int:
        proc = self._process
        while proc.returncode is None:  # noqa: ASYNC110 - no event exists for this
            await asyncio.sleep(EXIT_POLL)
        return proc.returncode

    @staticmethod
    def _signal_group(pgid: int, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)

    # ---------------------------------------------------------------- I/O

    async def _feed_stdin(self) -> None:
        stdin = self._process.stdin
        if stdin is None:
            return
        try:
            stdin.write(self._invocation.stdin)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the CLI exited or closed stdin early; its exit status tells the story
        finally:
            with contextlib.suppress(Exception):
                stdin.close()
                await stdin.wait_closed()

    async def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        cap = self._limits.max_stderr_bytes
        while chunk := await stderr.read(READ_CHUNK):
            self._stderr_total += len(chunk)
            self._stderr += chunk
            if len(self._stderr) > cap:
                del self._stderr[: len(self._stderr) - cap]

    def __aiter__(self) -> AsyncGenerator[bytes, None]:
        if self._line_iter is not None:
            raise InternalError("a run can only be iterated once")
        self._line_iter = self._lines()
        return self._line_iter

    async def _lines(self) -> AsyncGenerator[bytes, None]:
        stdout = self._process.stdout
        if stdout is None:
            raise InternalError("run started without a stdout pipe")
        loop = asyncio.get_running_loop()
        max_line = self._limits.max_line_bytes
        buf = bytearray()
        seen_line = False
        # Idle/first-output clocks run while *waiting* for the next line, not
        # while the consumer processes the previous one.
        waiting_since = loop.time()

        while chunk := await self._read_chunk(stdout, waiting_since, seen_line):
            buf += chunk
            while (newline := buf.find(b"\n")) >= 0:
                line = bytes(buf[:newline])
                del buf[: newline + 1]
                if len(line) > max_line:
                    await self._fail(OutputTooLargeError("line cap exceeded"), "line_cap")
                seen_line = True
                yield line.removesuffix(b"\r")
                waiting_since = loop.time()
            if len(buf) > max_line:
                await self._fail(OutputTooLargeError("line cap exceeded"), "line_cap")

        if buf:
            yield bytes(buf).removesuffix(b"\r")
        await self._wait_for_exit()

    async def _read_chunk(
        self, stdout: asyncio.StreamReader, waiting_since: float, seen_line: bool
    ) -> bytes:
        """Read the next stdout chunk under the applicable timeout and total cap."""
        limits = self._limits
        per_line = limits.idle_timeout if seen_line else limits.first_output_timeout
        line_deadline = waiting_since + per_line
        read = asyncio.ensure_future(stdout.read(READ_CHUNK))
        try:
            async with asyncio.timeout_at(min(line_deadline, self._deadline)):
                chunk = await self._read_or_exit(read)
        except TimeoutError:
            if self._deadline <= line_deadline:
                which = "total"
            else:
                which = "idle" if seen_line else "first_output"
            elapsed = self._now() - self._started
            await self._fail(ProviderTimeoutError(f"{which} timeout at {elapsed:.1f}s"), which)
        finally:
            if not read.done():
                read.cancel()
        self._stdout_total += len(chunk)
        if self._stdout_total > limits.max_stdout_bytes:
            await self._fail(OutputTooLargeError("stdout total cap exceeded"), "stdout_cap")
        return chunk

    async def _read_or_exit(self, read: asyncio.Future[bytes]) -> bytes:
        """Return the read result, or EOF if the CLI exited and the pipe stays quiet.

        A child left behind by the CLI can inherit stdout and keep it open
        after the CLI itself exits; without this we would wait for a timeout.
        """
        exit_task = self._exit_task
        if exit_task is not None and not exit_task.done():
            either: set[asyncio.Future[Any]] = {read, exit_task}
            await asyncio.wait(either, return_when=asyncio.FIRST_COMPLETED)
        if read.done():
            return read.result()
        try:
            async with asyncio.timeout(POST_EXIT_DRAIN):
                return await read
        except TimeoutError:
            return b""

    async def _wait_for_exit(self) -> None:
        """stdout closed: wait for exit within the remaining total budget."""
        try:
            async with asyncio.timeout_at(self._deadline):
                exit_code = await self._watch_exit()
        except TimeoutError:
            await self._fail(ProviderTimeoutError("process did not exit after EOF"), "total")
        # The CLI is done: kill anything it left in its group, so pipes close.
        await self.kill("finished")
        if self._stderr_task is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(STDERR_SETTLE):
                    await self._stderr_task
        self._result = self._make_result(exit_code)

    async def _fail(self, err: ModelMuxError, reason: str) -> NoReturn:
        await self.kill(reason)
        raise err

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def _make_result(self, exit_code: int) -> RunResult:
        return RunResult(
            exit_code=exit_code,
            stderr_tail=self.stderr_tail(),
            duration=self._now() - self._started,
            stdout_bytes=self._stdout_total,
        )


def run_argv(binary: Path, args: Sequence[str]) -> tuple[str, ...]:
    """Helper for drivers: ``argv`` with the resolved binary as ``argv[0]``."""
    return (str(binary), *args)


@dataclass(frozen=True)
class ProbeOutput:
    exit_code: int
    lines: tuple[bytes, ...]
    stderr_tail: str

    def text(self) -> str:
        """stdout and the stderr tail as one string, for simple pattern checks."""
        stdout = b"\n".join(self.lines).decode("utf-8", "replace")
        return f"{stdout}\n{self.stderr_tail}"


class ProbeContext:
    """Lets a driver's ``probe()`` run the CLI through the normal runtime path."""

    def __init__(self, runner: Runner, work_root: Path) -> None:
        self.runner = runner
        self.work_root = work_root

    @property
    def binary(self) -> Path:
        return self.runner.binary

    async def run(self, build: Callable[[Path], Invocation], *, budget: float) -> ProbeOutput:
        """Run one invocation (built for a fresh workspace) and collect its output.

        ``budget`` is the total seconds allowed; the runner kills the process
        group when it runs out. Runtime errors propagate as ``ModelMuxError``.
        """
        with create_workspace(self.work_root) as workspace:
            invocation = build(workspace.path)
            deadline = asyncio.get_running_loop().time() + budget
            async with self.runner.start(invocation, workspace, deadline=deadline) as run:
                lines = tuple([line async for line in run])
            return ProbeOutput(run.result.exit_code, lines, run.result.stderr_tail)
