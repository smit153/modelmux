"""Process inspection helpers for tests (Linux /proc)."""

from __future__ import annotations

import os
import time
from pathlib import Path


def _stat_fields(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # Fields after the ")" that closes the command name: state, ppid, pgrp, ...
    return raw.rsplit(")", 1)[1].split()


def alive(pid: int) -> bool:
    """True if ``pid`` exists and is not a zombie."""
    fields = _stat_fields(pid)
    return fields is not None and fields[0] != "Z"


def wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


def group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes whose process group is ``pgid``."""
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fields = _stat_fields(int(entry))
        if fields and fields[0] != "Z" and int(fields[2]) == pgid:
            members.append(int(entry))
    return members
