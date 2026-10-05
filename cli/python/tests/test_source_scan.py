"""No shells anywhere; only modelmux_cli/docker.py may start processes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "modelmux_cli"
RUNNER = SRC / "docker.py"
SPAWN_MODULES = {"subprocess", "pty", "multiprocessing"}
SPAWN_ATTRS = {"system", "popen", "spawnl", "spawnv", "spawnvp", "execv", "execvp", "fork",
               "forkpty", "posix_spawn", "startfile"}  # fmt: skip


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.keyword) and node.arg == "shell":
            found.append(f"{line}: shell= keyword")
        if path != RUNNER:
            if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] in SPAWN_MODULES for a in node.names
            ):
                found.append(f"{line}: imports a process module")
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] in SPAWN_MODULES
            ):
                found.append(f"{line}: imports from a process module")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in SPAWN_ATTRS
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                found.append(f"{line}: os.{node.attr}")
    return found


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_shells_or_stray_processes(path: Path) -> None:
    assert violations(path) == []


def test_scanner_catches_violations(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import subprocess\nimport os\nos.system('x')\nsubprocess.run('x', shell=True)\n"
    )
    found = violations(bad)
    assert len(found) == 3
