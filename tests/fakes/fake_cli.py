"""A scriptable fake CLI for integration tests (stdlib only).

Behaviour is chosen with ``FAKE_SCENARIO``. Other knobs:

- ``FAKE_OUT``: file path for scenarios that record things (env, argv, pids)
- ``FAKE_SIZE``: byte count for size scenarios
- ``FAKE_FIXTURE``: JSONL file to replay (``replay`` scenario)
- ``FAKE_DELAY``: seconds between replayed lines (default 0)

Claude mode (argv contains ``-p``) imitates ``claude -p --output-format
stream-json``: ``--version``, "unknown option" for ``FAKE_UNKNOWN_FLAG``,
the empty-input error, and these scenarios:

- ``claude_text`` (default): a synthetic stream replying ``FAKE_REPLY``
  (``__STDIN__`` replies with the prompt it received)
- ``claude_fixture``: replay ``FAKE_FIXTURE`` then exit ``FAKE_EXIT``;
  ``FAKE_HANG_AFTER=1`` sleeps afterwards instead of exiting
- any generic scenario above

Requests whose stdin contains ``modelmux-probe`` use ``FAKE_PROBE``
(default ``claude_text`` replying "ok") instead of ``FAKE_SCENARIO``.

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


INPUT_REQUIRED = (
    "Error: Input must be provided either through stdin or as a prompt argument when using --print"
)


def emit(event: dict[str, object]) -> None:
    out(json.dumps(event))


def record_invocation(stdin: bytes) -> None:
    cwd = Path.cwd()
    record(
        {
            "env": dict(os.environ),
            "argv": sys.argv,
            "cwd": str(cwd),
            "stdin": stdin.decode("utf-8", "replace"),
            "files": {
                p.name: {"mode": oct(p.stat().st_mode & 0o777), "text": p.read_text()}
                for p in sorted(cwd.iterdir())
                if p.is_file()
            },
        }
    )


def claude_text(reply: str) -> int:
    session = {"session_id": "fake", "uuid": "fake"}
    emit({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], **session})
    half = len(reply) // 2
    for chunk in (reply[:half], reply[half:]):
        if chunk:
            emit(
                {
                    "type": "stream_event",
                    "event": {"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": chunk}},
                    "parent_tool_use_id": None,
                    **session,
                }
            )  # fmt: skip
    emit(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]},
            "parent_tool_use_id": None,
            **session,
        }
    )
    result: dict[str, object] = {
        "type": "result", "subtype": "success", "is_error": False, "result": reply,
        "permission_denials": [], **session,
    }  # fmt: skip
    if not os.environ.get("FAKE_NO_USAGE"):
        result["usage"] = {
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
        }  # fmt: skip
    emit(result)
    return 0


def claude_mode() -> int:
    stdin = sys.stdin.buffer.read()
    if not stdin.strip():
        sys.stderr.write(INPUT_REQUIRED + "\n")
        return 1
    probe = b"modelmux-probe" in stdin
    if not probe:
        record_invocation(stdin)
    name = os.environ.get("FAKE_PROBE" if probe else "FAKE_SCENARIO", "claude_text")
    if name == "claude_text":
        reply = "ok" if probe else os.environ.get("FAKE_REPLY", "Hello from fake Claude.")
        if reply == "__STDIN__":
            reply = stdin.decode("utf-8", "replace")
        return claude_text(reply)
    if name == "claude_fixture":
        delay = float(os.environ.get("FAKE_DELAY", "0"))
        for raw in Path(os.environ["FAKE_FIXTURE"]).read_bytes().splitlines():
            if raw.strip():
                out(raw)
                if delay:
                    time.sleep(delay)
        if os.environ.get("FAKE_HANG_AFTER"):
            time.sleep(3600)
        return int(os.environ.get("FAKE_EXIT", "0"))
    fn = SCENARIOS.get(name)
    if fn is None:
        sys.stderr.write(f"unknown scenario {name}\n")
        return 64
    return int(fn())


SCENARIOS = {
    name.removeprefix("scenario_"): fn
    for name, fn in globals().items()
    if name.startswith("scenario_") and callable(fn)
}


def main() -> int:
    if "--version" in sys.argv:
        out(os.environ.get("FAKE_VERSION", "2.1.285 (Claude Code)"))
        return 0
    unknown = os.environ.get("FAKE_UNKNOWN_FLAG")
    if unknown and unknown in sys.argv:
        sys.stderr.write(f"error: unknown option '{unknown}'\n")
        return 1
    if "-p" in sys.argv:
        return claude_mode()
    name = os.environ.get("FAKE_SCENARIO", "echo")
    fn = SCENARIOS.get(name)
    if fn is None:
        sys.stderr.write(f"unknown scenario {name}\n")
        return 64
    result: int = fn()
    return result


if __name__ == "__main__":
    sys.exit(main())
