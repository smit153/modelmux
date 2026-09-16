"""Static checks: no shells anywhere, and only runtime/runner.py spawns processes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "modelmux"
RUNNER = SRC / "runtime" / "runner.py"

SPAWN_ATTRS = {
    "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
    "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe", "execv", "execve",
    "execvp", "execvpe", "posix_spawn", "posix_spawnp", "fork", "forkpty",
    "create_subprocess_exec", "create_subprocess_shell",
}  # fmt: skip
SHELL_ATTRS = {"system", "popen", "create_subprocess_shell"}
SPAWN_MODULES = {"subprocess", "pty", "multiprocessing"}
# Modules whose attributes can spawn processes (checked via any import alias).
ATTR_MODULES = {"os", "posix", "asyncio", "subprocess", "pty"}


def source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def violations(path: Path, root: Path = SRC) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    is_runner = path == RUNNER
    found: list[str] = []
    module_aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.split(".")[0] in ATTR_MODULES
    }
    for node in ast.walk(tree):
        where = f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.keyword) and node.arg == "shell":
            found.append(f"{where} shell= keyword")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in SPAWN_MODULES:
                    found.append(f"{where} import {alias.name}")
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in SPAWN_MODULES:
                found.append(f"{where} from {node.module} import")
            for alias in node.names:
                if alias.name in SPAWN_ATTRS and not (
                    is_runner and alias.name == "create_subprocess_exec"
                ):
                    found.append(f"{where} imports {alias.name}")
        if (
            isinstance(node, ast.Attribute)
            and node.attr in SPAWN_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ):
            allowed = is_runner and node.attr not in SHELL_ATTRS
            if not allowed:
                found.append(f"{where} uses .{node.attr}")
    return found


def test_scan_finds_sources() -> None:
    assert len(source_files()) > 5


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_shell_or_stray_process_spawning(path: Path) -> None:
    assert violations(path) == []


def test_scanner_detects_violations(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import subprocess\nimport os as o\nimport asyncio\n"
        "subprocess.run('ls', shell=True)\no.system('ls')\n"
        "asyncio.create_subprocess_exec('ls')\nself.system = 1\n"
    )
    found = violations(bad, root=tmp_path)
    assert any("import subprocess" in f for f in found)
    assert any("shell=" in f for f in found)
    assert any(".system" in f for f in found)
    assert any(".create_subprocess_exec" in f for f in found)
    assert len([f for f in found if ".system" in f]) == 1  # self.system is fine
