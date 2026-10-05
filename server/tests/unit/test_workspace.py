from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from modelmux.errors import InternalError
from modelmux.runtime.workspace import (
    PREFIX,
    WorkRootError,
    create_workspace,
    prepare_work_root,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "work"
    prepare_work_root(r)
    return r


def test_prepare_creates_private_root(tmp_path: Path) -> None:
    r = tmp_path / "a" / "b"
    assert prepare_work_root(r) == 0
    assert mode(r) == 0o700


def test_prepare_tightens_mode(tmp_path: Path) -> None:
    r = tmp_path / "loose"
    r.mkdir(mode=0o777)
    os.chmod(r, 0o777)  # noqa: S103 - deliberately loose, prepare must tighten it
    prepare_work_root(r)
    assert mode(r) == 0o700


def test_prepare_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(WorkRootError):
        prepare_work_root(link)


def test_prepare_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises((WorkRootError, FileExistsError)):
        prepare_work_root(f)


def test_prepare_removes_leftovers(root: Path) -> None:
    left = root / f"{PREFIX}crashed"
    (left / "sub").mkdir(parents=True)
    (left / "sub" / "f").write_text("x")
    os.chmod(left / "sub", 0o500)  # read-only dir left by a CLI
    (root / f"{PREFIX}stray-file").write_text("x")
    keep = root / "not-ours"
    keep.mkdir()
    assert prepare_work_root(root) == 2
    assert not left.exists()
    assert keep.exists()


def test_workspace_private_and_removed(root: Path) -> None:
    with create_workspace(root) as ws:
        assert ws.path.parent == root
        assert ws.path.name.startswith(PREFIX)
        assert mode(ws.path) == 0o700
        assert list(ws.path.iterdir()) == []
        f = ws.write_file("system-prompt.txt", b"hello")
        assert mode(f) == 0o600
        assert f.read_bytes() == b"hello"
        path = ws.path
    assert not path.exists()


def test_workspaces_are_unique(root: Path) -> None:
    with create_workspace(root) as a, create_workspace(root) as b:
        assert a.path != b.path


def test_removed_on_error(root: Path) -> None:
    seen: list[Path] = []

    def fail() -> None:
        with create_workspace(root) as ws:
            seen.append(ws.path)
            (ws.path / "nested").mkdir()
            os.chmod(ws.path / "nested", 0o500)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fail()
    assert not seen[0].exists()


async def test_removed_on_cancellation(root: Path) -> None:
    started = asyncio.Event()
    seen: list[Path] = []

    async def job() -> None:
        with create_workspace(root) as ws:
            seen.append(ws.path)
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not seen[0].exists()


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "x" * 65, "a\x00b"])
def test_write_file_rejects_bad_names(root: Path, name: str) -> None:
    with create_workspace(root) as ws, pytest.raises(InternalError):
        ws.write_file(name, b"x")


def test_write_file_never_overwrites_or_follows(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("original")
    with create_workspace(root) as ws:
        ws.write_file("once", b"1")
        with pytest.raises(FileExistsError):
            ws.write_file("once", b"2")
        (ws.path / "link").symlink_to(outside)
        with pytest.raises(OSError):  # noqa: PT011 - O_EXCL/O_NOFOLLOW both raise OSError
            ws.write_file("link", b"pwned")
    assert outside.read_text() == "original"
