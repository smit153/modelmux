"""Shared fixtures: a scripted fake Docker and an isolated CLI home."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modelmux_cli import context, redact
from modelmux_cli.console import Console
from modelmux_cli.docker import Result
from modelmux_cli.errors import DockerError

Response = tuple[int, str, str] | DockerError
Matcher = Callable[[tuple[str, ...]], bool]


class FakeDocker:
    """Records every docker call; answers from rules matched in order.

    ``when(*prefix)`` matches calls whose arguments start with ``prefix``
    (``compose`` calls are matched after ``compose -p <project> -f <file>``).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.passthrough_calls: list[tuple[str, ...]] = []
        self.rules: list[tuple[Matcher, Response]] = []
        self.server_version = "29.8.0"

    def when(self, *prefix: str, returns: Response = (0, "", "")) -> FakeDocker:
        self.rules.insert(0, (lambda args: args[: len(prefix)] == prefix, returns))
        return self

    def _answer(self, args: tuple[str, ...], key: tuple[str, ...], check: bool) -> Result:
        self.calls.append(args)
        response: Response = (0, "", "")
        for matcher, value in self.rules:
            if matcher(key):
                response = value
                break
        if isinstance(response, DockerError):
            raise response
        code, out, err = response
        if check and code != 0:
            raise DockerError(f"docker {args[0]} failed", hint="fake")
        return Result(args, code, out, err)

    def run(self, *args: str, check: bool = True, **_: Any) -> Result:
        return self._answer(args, args, check)

    def compose(self, project: str, file: Path, *args: str, check: bool = True, **_: Any) -> Result:
        full = ("compose", "-p", project, "-f", str(file), *args)
        return self._answer(full, ("compose", *args), check)

    def passthrough(self, *args: str, timeout: float | None = None) -> int:
        self.passthrough_calls.append(args)
        return 0

    def check_available(self) -> str:
        self.calls.append(("version",))
        return self.server_version

    def called(self, *prefix: str) -> list[tuple[str, ...]]:
        """Calls whose arguments (compose: after the project/file flags) start with prefix."""
        found = []
        for call in self.calls:
            key = ("compose", *call[5:]) if call[:1] == ("compose",) else call
            if key[: len(prefix)] == prefix:
                found.append(call)
        return found


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    docker = FakeDocker()
    monkeypatch.setattr(context, "make_docker", lambda _console: docker)
    return docker


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("MODELMUX_CLI_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _fresh_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact, "_secrets", set())


@pytest.fixture
def console() -> tuple[Console, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    return Console(out, err, color=False), out, err
