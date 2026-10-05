from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from tests.conftest import FakeDocker

from modelmux_cli import _pinned, health
from modelmux_cli.commands import up as up_module
from modelmux_cli.main import main

RUNNING = '{"Service": "modelmux-claude", "State": "running", "Health": "", "ExitCode": 0}'
EXITED = '{"Service": "modelmux-claude", "State": "exited", "Health": "", "ExitCode": 1}'


@pytest.fixture(autouse=True)
def _env(fake_docker: FakeDocker, cli_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pinned, "IMAGE", "ghcr.io/smit153/modelmux@sha256:" + "a" * 64)
    monkeypatch.setattr(up_module, "port_free", lambda _port: True)
    monkeypatch.setattr(up_module, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(health, "ready", lambda _port: True)
    fake_docker.when("volume", "inspect", returns=(0, "[]", ""))
    fake_docker.when("image", "inspect", returns=(0, "[]", ""))


def logged_in(docker: FakeDocker, *names: str) -> None:
    """Make the helper 'status' command succeed for these providers only."""
    volumes = [f"modelmux_{name}-home:" for name in names]

    def is_logged_in_helper(args: tuple[str, ...]) -> bool:
        return args[:1] == ("run",) and any(v in " ".join(args) for v in volumes)

    docker.when("run", returns=(1, "", ""))
    docker.rules.insert(0, (is_logged_in_helper, (0, "", "")))


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


# ------------------------------------------------------------------ up


def test_up_dev_build_needs_image(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_docker: FakeDocker
) -> None:
    monkeypatch.setattr(_pinned, "IMAGE", None)
    code, out = cli(capsys, "up")
    assert code == 2
    assert "--image" in out
    assert fake_docker.calls == []


def test_up_nothing_logged_in(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, cli_home: Path
) -> None:
    logged_in(fake_docker)
    code, out = cli(capsys, "up")
    assert code == 0
    assert "No provider is logged in yet" in out
    assert "modelmux login claude" in out
    assert not fake_docker.called("compose", "up")
    # Everything is prepared anyway.
    assert (cli_home / "compose.yaml").exists()
    assert (cli_home / "secrets.env").exists()
    assert "Created an API key" in out
    key = (cli_home / "secrets.env").read_text().split("=", 1)[1].strip()
    assert key not in out


def test_up_starts_logged_in_providers(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    logged_in(fake_docker, "claude")
    code, out = cli(capsys, "up")
    assert code == 0
    (call,) = fake_docker.called("compose", "up")
    assert call[-3:] == ("up", "-d", "modelmux-claude")
    assert "Claude Code is running at http://127.0.0.1:8101/v1" in out
    assert "OpenAI Codex is not logged in: modelmux login codex" in out


def test_up_is_idempotent(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    logged_in(fake_docker, "claude")
    cli(capsys, "up")
    fake_docker.when("compose", "ps", returns=(0, RUNNING, ""))
    code, out = cli(capsys, "up")
    assert code == 0
    assert "already running" in out
    assert "Created an API key" not in out


def test_up_explicit_provider_not_logged_in(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    logged_in(fake_docker, "claude")
    code, out = cli(capsys, "up", "codex")
    assert code == 1
    assert "OpenAI Codex is not logged in yet." in out
    assert "modelmux login codex" in out
    assert not fake_docker.called("compose", "up")


def test_up_unknown_provider(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = cli(capsys, "up", "gemini")
    assert code == 2
    assert "Unknown provider 'gemini'" in out


def test_up_port_in_use(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged_in(fake_docker, "claude")
    monkeypatch.setattr(up_module, "port_free", lambda port: port != 8101)
    code, out = cli(capsys, "up")
    assert code == 1
    assert "Port 8101 for Claude Code is already in use" in out
    assert "--port claude=<port>" in out
    assert not fake_docker.called("compose", "up")


def test_up_port_and_image_are_remembered(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, cli_home: Path
) -> None:
    logged_in(fake_docker, "claude")
    code, out = cli(capsys, "up", "--port", "claude=9101", "--image", "modelmux:dev")
    assert code == 0
    assert "127.0.0.1:9101" in out
    config = json.loads((cli_home / "config.json").read_text())
    assert config == {"version": 1, "ports": {"claude": 9101}, "image": "modelmux:dev"}
    compose = json.loads((cli_home / "compose.yaml").read_text().split("\n", 1)[1])
    assert compose["services"]["modelmux-claude"]["image"] == "modelmux:dev"


@pytest.mark.parametrize(
    "value", ["claude", "claude=80", "claude=abc", "gemini=9000", "=9000", "claude=70000"]
)
def test_up_invalid_port(capsys: pytest.CaptureFixture[str], value: str) -> None:
    code, out = cli(capsys, "up", "--port", value)
    assert code == 2
    assert "Invalid --port" in out


def test_up_duplicate_ports(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = cli(capsys, "up", "--port", "claude=8102")
    assert code == 2
    assert "same port" in out


def test_up_container_stops_while_starting(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged_in(fake_docker, "claude")
    monkeypatch.setattr(health, "ready", lambda _port: False)
    fake_docker.when("compose", "ps", returns=(0, EXITED, ""))
    code, out = cli(capsys, "up")
    assert code == 1
    assert "stopped while starting" in out
    assert "modelmux login claude" in out


def test_up_not_ready_in_time(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged_in(fake_docker, "claude")
    monkeypatch.setattr(health, "ready", lambda _port: False)
    monkeypatch.setattr(up_module, "READY_TIMEOUT", 0.0)
    code, out = cli(capsys, "up")
    assert code == 1
    assert "did not become ready in time" in out
    assert "modelmux logs claude" in out


def test_up_pulls_missing_image(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    logged_in(fake_docker)
    fake_docker.when("image", "inspect", returns=(1, "", ""))
    code, out = cli(capsys, "up")
    assert code == 0
    assert "Downloading the ModelMux server image" in out
    assert fake_docker.called("pull")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_up_files_are_private(capsys: pytest.CaptureFixture[str], cli_home: Path) -> None:
    cli(capsys, "up")
    for name in ("compose.yaml", "secrets.env"):
        assert stat.S_IMODE((cli_home / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(cli_home.stat().st_mode) == 0o700


# ------------------------------------------------------------------ down / logs / status


def test_down_when_not_set_up(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    code, out = cli(capsys, "down")
    assert code == 0
    assert "not running" in out
    assert fake_docker.calls == []


def test_down_keeps_logins(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    cli(capsys, "up")
    code, out = cli(capsys, "down")
    assert code == 0
    (call,) = fake_docker.called("compose", "down")
    assert "-v" not in call
    assert "--volumes" not in call
    assert "logins are kept" in out


def test_logs_not_set_up(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = cli(capsys, "logs")
    assert code == 1
    assert "not set up" in out


def test_logs_passthrough(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    cli(capsys, "up")
    assert cli(capsys, "logs", "claude", "-f", "--tail", "5")[0] == 0
    (args,) = fake_docker.passthrough_calls
    assert args[:3] == ("compose", "-p", "modelmux")
    assert args[5:] == ("logs", "--no-log-prefix", "--tail", "5", "--follow", "modelmux-claude")


def test_status_table(capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker) -> None:
    logged_in(fake_docker, "claude")
    cli(capsys, "up")
    fake_docker.when("compose", "ps", returns=(0, RUNNING, ""))
    code, out = cli(capsys, "status")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["PROVIDER", "CONTAINER", "LOGIN", "HEALTH", "URL"]
    assert lines[1].split() == ["claude", "running", "yes", "ready", "http://127.0.0.1:8101/v1"]
    assert lines[2].split() == ["codex", "not", "started", "no", "-", "-"]


def test_status_reports_stopped_container(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    cli(capsys, "up")
    fake_docker.when("compose", "ps", returns=(0, EXITED, ""))
    code, out = cli(capsys, "status")
    assert code == 0
    assert "Claude Code has stopped" in out
    assert "modelmux login claude" in out
