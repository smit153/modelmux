from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path

import pytest

from modelmux_cli import _pinned, health
from modelmux_cli.commands import login as login_module
from modelmux_cli.commands import logout as logout_module
from modelmux_cli.commands import up as up_module
from modelmux_cli.main import main
from tests.conftest import FakeDocker
from tests.test_terminal import CLAUDE_URL, FIXTURES

REAL_USE_PTY = login_module.use_pty  # before the autouse fixture replaces it
RUNNING = '{"Service": "modelmux-claude", "State": "running", "Health": "", "ExitCode": 0}'


class Session:
    """Whether each provider counts as logged in (the helper 'status' answer)."""

    def __init__(self, docker: FakeDocker) -> None:
        self.logged_in: set[str] = set()
        docker.when("run", returns=(0, "", ""))
        docker.rules.insert(0, (self._is_status, (1, "", "")))
        self.docker = docker

    def _is_status(self, args: tuple[str, ...]) -> bool:
        if args[:1] != ("run",) or "status" not in args:
            return False
        joined = " ".join(args)
        return not any(f"modelmux_{name}-home:" in joined for name in self.logged_in)


@pytest.fixture
def session(fake_docker: FakeDocker) -> Session:
    return Session(fake_docker)


@pytest.fixture(autouse=True)
def _env(fake_docker: FakeDocker, cli_home: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(_pinned, "IMAGE", "ghcr.io/smit153/modelmux@sha256:" + "a" * 64)
    monkeypatch.setattr(up_module, "port_free", lambda _port: True)
    monkeypatch.setattr(up_module, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(health, "ready", lambda _port: True)
    monkeypatch.setattr(login_module, "use_pty", lambda _args: True)
    fake_docker.when("volume", "inspect", returns=(0, "[]", ""))
    fake_docker.when("image", "inspect", returns=(0, "[]", ""))
    opened: list[str] = []
    monkeypatch.setattr(login_module, "open_browser", lambda url: opened.append(url) or True)
    return opened


@pytest.fixture
def opened(_env: list[str]) -> list[str]:
    return _env


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def log_in_when_run(fake_docker: FakeDocker, session: Session, name: str) -> None:
    fake_docker.on_interactive = lambda: session.logged_in.add(name)


# ------------------------------------------------------------------ login


def test_pty_login_opens_browser_and_tests(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session,
    opened: list[str],
) -> None:  # fmt: skip
    fake_docker.pty_output = (FIXTURES / "claude_browser.raw").read_bytes()
    log_in_when_run(fake_docker, session, "claude")
    code, out = cli(capsys, "login", "claude")
    assert code == 0, out
    (args,) = fake_docker.pty_calls
    assert args[:4] == ("run", "--rm", "-it", "--name")
    assert args[4].startswith("modelmux-login-claude-")
    assert args[args.index("--entrypoint") + 1] == "claude"
    assert args[-2:] == ("auth", "login")
    assert "--read-only" in args
    assert "modelmux_claude-home:/home/modelmux/driver-home" in args
    assert opened == [CLAUDE_URL]
    assert any(b"Opened your browser" in extra for extra in fake_docker.pty_extra)
    (up,) = fake_docker.called("compose", "up")
    assert up[-3:] == ("-d", "--force-recreate", "modelmux-claude")
    assert "Logged in to Claude Code." in out
    assert "Claude Code is logged in and tested." in out


def test_no_browser(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session,
    opened: list[str],
) -> None:  # fmt: skip
    fake_docker.pty_output = (FIXTURES / "codex_device.raw").read_bytes()
    log_in_when_run(fake_docker, session, "codex")
    code, _ = cli(capsys, "login", "codex", "--no-browser")
    assert code == 0
    assert opened == []
    assert any(b"Open the link above" in extra for extra in fake_docker.pty_extra)


def test_browser_unavailable(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(login_module, "open_browser", lambda _url: False)
    fake_docker.pty_output = (FIXTURES / "codex_device.raw").read_bytes()
    log_in_when_run(fake_docker, session, "codex")
    assert cli(capsys, "login", "codex")[0] == 0
    assert any(b"Could not open a browser" in extra for extra in fake_docker.pty_extra)


def test_already_logged_in(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    session.logged_in.add("claude")
    code, out = cli(capsys, "login", "claude")
    assert code == 0
    assert "already logged in" in out
    assert fake_docker.pty_calls == []
    assert "logged in and tested" in out


def test_already_logged_in_and_running(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    session.logged_in.add("claude")
    cli(capsys, "up")
    fake_docker.when("compose", "ps", returns=(0, RUNNING, ""))
    fake_docker.calls.clear()
    code, out = cli(capsys, "login", "claude")
    assert code == 0
    assert not fake_docker.called("compose", "up")
    assert "Claude Code is running at http://127.0.0.1:8101/v1" in out


def test_force_logs_in_again(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    session.logged_in.add("claude")
    code, _ = cli(capsys, "login", "claude", "--force")
    assert code == 0
    assert len(fake_docker.pty_calls) == 1


def test_login_not_completed(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    fake_docker.pty_result = 1
    code, out = cli(capsys, "login", "claude")
    assert code == 1
    assert "login did not complete" in out
    assert "--raw" in out
    assert not fake_docker.called("compose", "up")


def test_timeout_removes_helper(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    fake_docker.pty_result = TimeoutError()
    code, out = cli(capsys, "login", "claude")
    assert code == 1
    assert "timed out" in out
    name = fake_docker.pty_calls[0][4]
    assert fake_docker.called("rm", "-f", name)


def test_ctrl_c_removes_helper(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    fake_docker.pty_result = KeyboardInterrupt()
    code, out = cli(capsys, "login", "claude")
    assert code == 130
    assert "Cancelled." in out
    assert fake_docker.called("rm", "-f", fake_docker.pty_calls[0][4])


def test_raw_or_windows_uses_the_console_directly(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(login_module, "use_pty", lambda _args: False)
    log_in_when_run(fake_docker, session, "claude")
    code, out = cli(capsys, "login", "claude", "--raw")
    assert code == 0
    assert fake_docker.pty_calls == []
    (args,) = fake_docker.passthrough_calls
    assert args[2] == "-it"
    assert "open the link they show in your browser" in out


def test_use_pty_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    raw, plain = argparse.Namespace(raw=True), argparse.Namespace(raw=False)
    assert REAL_USE_PTY(raw) is False
    monkeypatch.setattr(login_module.os, "name", "nt")
    assert REAL_USE_PTY(plain) is False
    monkeypatch.setattr(login_module.os, "name", "posix")
    assert REAL_USE_PTY(plain) is True


def test_api_key_login(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    prompts: list[str] = []

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        session.logged_in.add("codex")
        return "sk-proj-SECRETKEYVALUE123456"

    monkeypatch.setattr(login_module, "read_secret", read_secret)
    code, out = cli(capsys, "login", "codex", "--method", "api-key")
    assert code == 0
    assert prompts == ["OpenAI API key (input is hidden): "]
    (call,) = [c for c in fake_docker.called("run") if "--with-api-key" in c]
    assert call[2] == "-i"
    assert "-t" not in call
    assert "sk-proj-SECRETKEYVALUE123456" not in " ".join(call)
    assert "SECRETKEYVALUE" not in out
    assert fake_docker.pty_calls == []


def test_api_key_empty(
    capsys: pytest.CaptureFixture[str], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_module, "read_secret", lambda _p: "  ")
    code, out = cli(capsys, "login", "codex", "--method", "api-key")
    assert code == 2
    assert "No OpenAI API key entered" in out


def test_unknown_method(capsys: pytest.CaptureFixture[str], session: Session) -> None:
    code, out = cli(capsys, "login", "claude", "--method", "magic")
    assert code == 2
    assert "browser (" in out
    assert "console (" in out


def test_login_needs_an_image(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_pinned, "IMAGE", None)
    code, out = cli(capsys, "login", "claude")
    assert code == 2
    assert "--image" in out
    assert fake_docker.calls == []


# ------------------------------------------------------------------ logout


def test_logout_nothing_to_remove(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    fake_docker.when("volume", "inspect", returns=(1, "", ""))
    code, out = cli(capsys, "logout", "claude", "--yes")
    assert code == 0
    assert "nothing to remove" in out
    assert not fake_docker.called("volume", "rm")


def test_logout_removes_only_that_login(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, session: Session
) -> None:
    cli(capsys, "up")
    fake_docker.calls.clear()
    code, out = cli(capsys, "logout", "claude", "--yes")
    assert code == 0
    assert fake_docker.called("compose", "rm", "--stop", "--force", "modelmux-claude")
    (logout,) = [c for c in fake_docker.called("run") if c[-2:] == ("auth", "logout")]
    assert "modelmux_claude-home:/home/modelmux/driver-home" in logout
    assert fake_docker.called("volume", "rm") == [("volume", "rm", "modelmux_claude-home")]
    assert "Logged out of Claude Code" in out


def test_logout_still_removes_when_provider_logout_fails(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker
) -> None:
    fake_docker.when("run", returns=(1, "", "network down"))
    code, _ = cli(capsys, "logout", "codex", "--yes")
    assert code == 0
    assert fake_docker.called("volume", "rm", "modelmux_codex-home")


def test_logout_asks_first(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logout_module.sys.stdin, "isatty", lambda: True, raising=False)
    questions: list[str] = []
    monkeypatch.setattr(logout_module, "ask", lambda q: questions.append(q) or "n")
    code, out = cli(capsys, "logout", "claude")
    assert code == 0
    assert questions == ["Remove the saved Claude Code login? [y/N] "]
    assert "Nothing changed." in out
    assert not fake_docker.called("volume", "rm")
    monkeypatch.setattr(logout_module, "ask", lambda _q: "yes")
    assert cli(capsys, "logout", "claude")[0] == 0
    assert fake_docker.called("volume", "rm")


def test_logout_without_terminal_needs_yes(
    capsys: pytest.CaptureFixture[str], fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logout_module.sys.stdin, "isatty", lambda: False, raising=False)
    code, out = cli(capsys, "logout", "claude")
    assert code == 2
    assert "--yes" in out
    assert not fake_docker.called("volume", "rm")


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_termination_signals_cancel_like_ctrl_c(sig: str) -> None:
    signum = getattr(signal, sig)
    before = signal.getsignal(signum)

    def receive_signal() -> None:
        with login_module.cancel_on_signals():
            os.kill(os.getpid(), signum)
            time.sleep(1)  # the handler interrupts this

    with pytest.raises(KeyboardInterrupt):
        receive_signal()
    assert signal.getsignal(signum) == before
