from __future__ import annotations

import io
import json
import os
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from modelmux_cli import __version__, _pinned, health, updates
from modelmux_cli.commands import doctor as doctor_module
from modelmux_cli.commands import up as up_module
from modelmux_cli.errors import DockerError
from modelmux_cli.main import main
from tests.conftest import FakeDocker

# The real functions, before the autouse fixture replaces them.
REAL_LATEST_VERSION = updates.latest_version
REAL_REGISTRY_REACHABLE = updates.registry_reachable
RUNNING = '{"Service": "modelmux-claude", "State": "running", "Health": "", "ExitCode": 0}'
EXITED = '{"Service": "modelmux-claude", "State": "exited", "Health": "", "ExitCode": 3}'


@pytest.fixture(autouse=True)
def _env(fake_docker: FakeDocker, cli_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pinned, "IMAGE", "ghcr.io/smit153/modelmux@sha256:" + "a" * 64)
    monkeypatch.setattr(up_module, "port_free", lambda _port: True)
    monkeypatch.setattr(doctor_module, "port_free", lambda _port: True)
    monkeypatch.setattr(up_module, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(health, "ready", lambda _port: True)
    monkeypatch.setattr(health, "get_json", lambda *_a, **_k: (200, {"data": []}))
    monkeypatch.setattr(updates, "registry_reachable", lambda: True)
    monkeypatch.setattr(updates, "latest_version", lambda: __version__)
    fake_docker.when("volume", "inspect", returns=(0, "[]", ""))
    fake_docker.when("image", "inspect", returns=(0, "[]", ""))
    # Claude logged in, Codex not.
    fake_docker.when("run", returns=(1, "", ""))
    fake_docker.rules.insert(
        0, (lambda a: a[:1] == ("run",) and "modelmux_claude-home:/home/modelmux/driver-home" in a,
            (0, "", "")),
    )  # fmt: skip


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def set_up(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, ps: str = RUNNING) -> None:
    assert cli(capsys, "up")[0] == 0
    fake_docker.when("compose", "ps", returns=(0, ps, ""))


# ------------------------------------------------------------------ doctor


def test_doctor_all_good(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    set_up(capsys, fake_docker)
    code, out = cli(capsys, "doctor")
    assert code == 0
    assert "Docker 29.8.0 is running" in out
    assert "Claude Code: logged in, running and answering" in out
    assert "OpenAI Codex: not logged in" in out
    assert "No problems" in out


def test_doctor_docker_not_running(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> str:
        raise DockerError("Docker is installed but not running.", hint="Start Docker Desktop.")

    monkeypatch.setattr(fake_docker, "check_available", broken)
    code, out = cli(capsys, "doctor")
    assert code == 1
    assert "not running" in out
    assert "Start Docker Desktop." in out


def test_doctor_not_set_up(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = cli(capsys, "doctor")
    assert code == 1
    assert "not set up" in out


def test_doctor_offline_and_newer_cli(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(capsys, fake_docker)
    monkeypatch.setattr(updates, "registry_reachable", lambda: False)
    monkeypatch.setattr(updates, "latest_version", lambda: "99.0.0")
    code, out = cli(capsys, "doctor")
    assert code == 0  # notes, not failures
    assert "Cannot reach ghcr.io" in out
    assert "HTTPS_PROXY" in out
    assert "modelmux-cli 99.0.0 is available" in out
    assert "pipx upgrade modelmux-cli" in out


@pytest.mark.parametrize(
    ("reason", "hint"),
    [
        ("live check failed: auth", "modelmux login claude --force"),
        ("CLI version 3.0.0 is not in >=2.1,<3", "modelmux upgrade"),
        ("CLI does not support lockdown flag --x", "modelmux upgrade"),
        (None, "modelmux logs claude"),
    ],
)
def test_doctor_explains_a_stopped_server(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, reason: str | None, hint: str
) -> None:
    set_up(capsys, fake_docker, EXITED)
    logs = ['{"event": "startup"}', "not json"]
    if reason:
        logs.append(json.dumps({"event": "probe_failed", "reason": reason}))
    fake_docker.when("compose", "logs", returns=(0, "\n".join(logs), ""))
    code, out = cli(capsys, "doctor")
    assert code == 1
    assert "Claude Code: the server stopped" in out
    if reason:
        assert reason in out
    assert hint in out


def test_doctor_wrong_api_key(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(capsys, fake_docker)
    monkeypatch.setattr(health, "get_json", lambda *_a, **_k: (401, None))
    code, out = cli(capsys, "doctor")
    assert code == 1
    assert "does not accept the saved API key" in out


def test_doctor_port_conflict(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(capsys, fake_docker, "")
    monkeypatch.setattr(doctor_module, "port_free", lambda port: port != 8101)
    code, out = cli(capsys, "doctor")
    assert code == 1
    assert "port 8101 is used by another program" in out


def test_doctor_missing_image(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    set_up(capsys, fake_docker)
    fake_docker.when("image", "inspect", returns=(1, "", ""))
    assert "is not downloaded yet" in cli(capsys, "doctor")[1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_doctor_loose_permissions(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, cli_home: Path
) -> None:
    set_up(capsys, fake_docker)
    (cli_home / "secrets.env").chmod(0o644)
    code, out = cli(capsys, "doctor")
    assert code == 1
    assert "readable by other users" in out
    assert "chmod 600" in out


# ------------------------------------------------------------------ upgrade


def test_upgrade_recreates_running_providers(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    set_up(capsys, fake_docker)
    fake_docker.calls.clear()
    code, out = cli(capsys, "upgrade")
    assert code == 0
    (call,) = fake_docker.called("compose", "up")
    assert call[-1] == "modelmux-claude"
    assert "Logins were kept." in out
    assert not fake_docker.called("volume", "rm")


def test_upgrade_with_new_image(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, cli_home: Path
) -> None:
    set_up(capsys, fake_docker)
    code, out = cli(capsys, "upgrade", "--image", "ghcr.io/smit153/modelmux:9.9.9")
    assert code == 0
    assert "Server image: ghcr.io/smit153/modelmux:9.9.9" in out
    assert json.loads((cli_home / "config.json").read_text())["image"].endswith(":9.9.9")
    compose = json.loads((cli_home / "compose.yaml").read_text().split("\n", 1)[1])
    assert compose["services"]["modelmux-claude"]["image"].endswith(":9.9.9")


def test_upgrade_nothing_running(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    code, out = cli(capsys, "upgrade")
    assert code == 0
    assert "Nothing was running" in out
    assert not fake_docker.called("compose", "up")


def test_upgrade_mentions_newer_cli(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "latest_version", lambda: "99.0.0")
    assert "modelmux-cli 99.0.0 is available" in cli(capsys, "upgrade")[1]


# ------------------------------------------------------------------ updates module


@pytest.mark.parametrize(
    ("current", "latest", "newer"),
    [("0.1.0", "0.2.0", True), ("0.1.0", "0.1.0", False), ("0.2.0", "0.1.9", False),
     ("0.1.0", "0.2.0rc1", False), ("0.1.0", None, False), ("0.1.0", "1.0", True)],
)  # fmt: skip
def test_newer_available(current: str, latest: str | None, newer: bool) -> None:
    assert updates.newer_available(current, latest) is newer


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_latest_version(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"info": {"version": "1.2.3"}}).encode()
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(body))
    assert REAL_LATEST_VERSION() == "1.2.3"


@pytest.mark.parametrize(
    "exc", [urllib.error.URLError("offline"), OSError("x"), ValueError("bad json")]
)
def test_latest_version_offline(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fail(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)
    assert REAL_LATEST_VERSION() is None


@pytest.mark.parametrize("exc", [urllib.error.URLError("offline"), OSError("x")])
def test_registry_unreachable(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fail(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)
    assert REAL_REGISTRY_REACHABLE() is False


def test_registry_reachable_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def unauthorized(*_a: Any, **_k: Any) -> Any:
        raise urllib.error.HTTPError(updates.REGISTRY_URL, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(updates.urllib.request, "urlopen", unauthorized)
    assert REAL_REGISTRY_REACHABLE() is True
