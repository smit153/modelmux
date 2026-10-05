"""Small, safe file writes: atomic, and private (0600) from the first byte."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

PRIVATE_MODE = 0o600


def write_private(path: Path, data: str) -> None:
    """Atomically replace ``path`` with ``data``, created with mode 0600.

    The temporary file is created with 0600 (never readable by others, even
    briefly) in the same directory, then renamed over the target.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def tighten(path: Path) -> bool:
    """Make an existing file 0600 on POSIX. Returns True if it had to change."""
    if os.name != "posix":
        return False
    mode = path.stat().st_mode & 0o777
    if mode != PRIVATE_MODE:
        path.chmod(PRIVATE_MODE)
        return True
    return False
