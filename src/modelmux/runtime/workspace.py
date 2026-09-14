"""Private, empty, per-request working directories."""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from modelmux.errors import InternalError

log = logging.getLogger("modelmux.runtime.workspace")

PREFIX = "mmx-"
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")


class WorkRootError(Exception):
    """``WORK_ROOT`` is unusable. The message is safe to print."""


class Workspace:
    """A fresh 0700 directory. Files written into it are 0600 and never followed."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write_file(self, name: str, data: bytes) -> Path:
        if not _FILE_NAME_RE.fullmatch(name):
            raise InternalError(f"invalid workspace file name {name!r}")
        target = self.path / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return target


def prepare_work_root(root: Path) -> int:
    """Create/verify ``root`` and remove leftovers from crashed runs.

    Returns the number of leftover workspaces removed.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkRootError("MODELMUX_WORK_ROOT must be a real directory, not a symlink")
    if info.st_uid != os.geteuid():
        raise WorkRootError("MODELMUX_WORK_ROOT must be owned by the ModelMux user")
    os.chmod(root, 0o700)

    removed = 0
    for entry in root.iterdir():
        if entry.name.startswith(PREFIX):
            _remove_tree(entry)
            removed += 1
    if removed:
        log.warning(
            "removed leftover workspaces", extra={"event": "workspace_cleanup", "count": removed}
        )
    return removed


@contextmanager
def create_workspace(root: Path) -> Iterator[Workspace]:
    """Yield a new private workspace under ``root``; always remove it afterwards."""
    path = Path(tempfile.mkdtemp(prefix=PREFIX, dir=root))  # mode 0700, random name
    try:
        yield Workspace(path)
    finally:
        _remove_tree(path)


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(func: Callable[..., Any], target: str, _exc: BaseException) -> None:
        # The CLI may have left read-only directories behind.
        parent = os.path.dirname(target)
        os.chmod(parent, stat.S_IRWXU)
        if os.path.isdir(target) and not os.path.islink(target):
            os.chmod(target, stat.S_IRWXU)
        func(target)

    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, onexc=make_writable_and_retry)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("failed to remove workspace", extra={"event": "workspace_cleanup_failed"})
