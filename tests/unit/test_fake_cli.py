"""Sanity checks for the fake CLI itself (run directly, not via the runner)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.fakes import install_fake_cli


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    return install_fake_cli(tmp_path / "bin")


def run(
    fake: Path, scenario: str, stdin: bytes = b"", **env: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(fake)],
        input=stdin,
        capture_output=True,
        env={"FAKE_SCENARIO": scenario, **env},
        timeout=10,
        check=False,
    )


def test_echo(fake: Path) -> None:
    result = run(fake, "echo", b"a\nb\n")
    assert result.stdout == b"a\nb\n"
    assert result.returncode == 0


def test_version(fake: Path) -> None:
    result = subprocess.run([str(fake), "--version"], capture_output=True, check=True, env={})
    assert b"fake" in result.stdout


def test_dump(fake: Path, tmp_path: Path) -> None:
    target = tmp_path / "dump.json"
    run(fake, "dump", b"xyz", FAKE_OUT=str(target))
    data = json.loads(target.read_text())
    assert data["stdin_bytes"] == 3
    assert data["env"]["FAKE_SCENARIO"] == "dump"


def test_exit_code(fake: Path) -> None:
    result = run(fake, "exit_code")
    assert result.returncode == 3
    assert b"STDERR_SENTINEL" in result.stderr


def test_unknown_scenario(fake: Path) -> None:
    assert run(fake, "nope").returncode == 64


def test_replay(fake: Path, tmp_path: Path) -> None:
    fixture = tmp_path / "f.jsonl"
    fixture.write_text('{"a": 1}\n\n{"b": 2}\n')
    result = run(fake, "replay", FAKE_FIXTURE=str(fixture), FAKE_EXIT="2")
    assert result.stdout == b'{"a": 1}\n{"b": 2}\n'
    assert result.returncode == 2
