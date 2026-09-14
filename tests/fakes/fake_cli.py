"""A scriptable fake CLI for integration tests (stdlib only).

Behaviour is chosen with ``FAKE_SCENARIO``. Other knobs:

- ``FAKE_OUT``: file path for scenarios that record things (env, argv, pids)
- ``FAKE_SIZE``: byte count for size scenarios
- ``FAKE_FIXTURE``: JSONL file to replay (``replay`` scenario)
- ``FAKE_DELAY``: seconds between replayed lines (default 0)

It is launched through a wrapper script (see ``tests/fakes/__init__.py``)
so it runs with the test interpreter under the runner's minimal environment.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def out(line: str | bytes) -> None:
    data = line.encode() if isinstance(line, str) else line
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def record(payload: dict[str, object]) -> None:
    target = os.environ.get("FAKE_OUT")
    if target:
        Path(target).write_text(json.dumps(payload))


def size() -> int:
    return int(os.environ.get("FAKE_SIZE", "1024"))


def scenario_echo() -> int:
    data = sys.stdin.buffer.read()
    for line in data.splitlines():
        out(line)
    return 0


def scenario_dump() -> int:
    stdin = sys.stdin.buffer.read()
    cwd = Path.cwd()
    record(
        {
            "env": dict(os.environ),
            "argv": sys.argv,
            "cwd": str(cwd),
            "stdin_bytes": len(stdin),
            "files": {
                p.name: {
                    "mode": oct(p.stat().st_mode & 0o777),
                    "text": p.read_text(errors="replace")[:4096],
                }
                for p in sorted(cwd.iterdir())
                if p.is_file()
            },
            "pgid": os.getpgid(0),
            "pid": os.getpid(),
            "sid": os.getsid(0),
        }
    )
    out("dumped")
    return 0


def scenario_lines() -> int:
    delay = float(os.environ.get("FAKE_DELAY", "0"))
    for i in range(size()):
        out(f"line {i}")
        if delay:
            time.sleep(delay)
    return 0


def scenario_replay() -> int:
    delay = float(os.environ.get("FAKE_DELAY", "0"))
    sys.stdin.buffer.read()
    for raw in Path(os.environ["FAKE_FIXTURE"]).read_bytes().splitlines():
        if raw.strip():
            out(raw)
            if delay:
                time.sleep(delay)
    return int(os.environ.get("FAKE_EXIT", "0"))


def scenario_hang_before_output() -> int:
    time.sleep(3600)
    return 0


def scenario_hang_mid_stream() -> int:
    out("first")
    time.sleep(3600)
    return 0


def scenario_slow_forever() -> int:
    # Never idle, never finishes: only the total timeout stops it.
    while True:
        out("tick")
        time.sleep(0.05)


def scenario_partial_line_trickle() -> int:
    # Bytes keep coming but never a full line: idle is measured per line.
    while True:
        sys.stdout.buffer.write(b"x")
        sys.stdout.buffer.flush()
        time.sleep(0.05)


def scenario_long_line() -> int:
    sys.stdout.buffer.write(b"a" * size())
    sys.stdout.buffer.flush()
    time.sleep(3600)
    return 0


def scenario_big_output() -> int:
    chunk = b"b" * 1000
    written = 0
    while written < size():
        out(chunk)
        written += len(chunk) + 1
    time.sleep(3600)
    return 0


def scenario_stderr_flood() -> int:
    block = b"E" * 65536
    for _ in range(size() // len(block) + 1):
        sys.stderr.buffer.write(block)
    sys.stderr.buffer.write(b"\nSTDERR_TAIL_MARKER\n")
    sys.stderr.buffer.flush()
    out("done")
    return 0


def scenario_exit_code() -> int:
    out("partial")
    sys.stderr.write("STDERR_SENTINEL something failed at /secret/path\n")
    sys.stderr.flush()
    return 3


def scenario_no_trailing_newline() -> int:
    sys.stdout.buffer.write(b"first\nlast-without-newline")
    sys.stdout.buffer.flush()
    return 0


def scenario_crlf() -> int:
    sys.stdout.buffer.write(b"one\r\ntwo\r\n")
    sys.stdout.buffer.flush()
    return 0


def scenario_ignore_term_child() -> int:
    """Spawn a grandchild that ignores SIGTERM, and ignore SIGTERM ourselves."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3600)",
        ],
    )
    record({"pid": os.getpid(), "child_pid": child.pid})
    out(f"child {child.pid}")
    time.sleep(3600)
    return 0


def scenario_orphan_child_then_exit() -> int:
    """Leave a detached-looking child in the group and exit 0 immediately."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
    )
    record({"pid": os.getpid(), "child_pid": child.pid})
    out("bye")
    return 0


def scenario_ignore_stdin() -> int:
    # Exit without reading stdin (the runner must tolerate a broken pipe).
    out("ignored stdin")
    return 0


SCENARIOS = {
    name.removeprefix("scenario_"): fn
    for name, fn in globals().items()
    if name.startswith("scenario_") and callable(fn)
}


def main() -> int:
    name = os.environ.get("FAKE_SCENARIO", "echo")
    if "--version" in sys.argv:
        out(os.environ.get("FAKE_VERSION", "9.9.9 (fake)"))
        return 0
    fn = SCENARIOS.get(name)
    if fn is None:
        sys.stderr.write(f"unknown scenario {name}\n")
        return 64
    result: int = fn()
    return result


if __name__ == "__main__":
    sys.exit(main())
