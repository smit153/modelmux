from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import pytest

from modelmux_cli import redact as redact_module
from modelmux_cli.console import Console
from modelmux_cli.docker import Docker, classify
from modelmux_cli.errors import EXIT_DOCKER, DockerError
from modelmux_cli.redact import register_secret


class FakeRunner:
    """Records calls; returns scripted results keyed by the first docker argument."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.results: dict[str, tuple[int, str, str]] = {}
        self.raises: BaseException | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        code, out, err = self.results.get(argv[1], (0, "", ""))
        return subprocess.CompletedProcess(argv, code, out, err)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def make(runner: FakeRunner, *, verbose: bool = False, which: str | None = "/usr/bin/docker",
         platform: str = "linux") -> tuple[Docker, io.StringIO]:  # fmt: skip
    err = io.StringIO()
    console = Console(io.StringIO(), err, verbose=verbose, color=False)
    return Docker(console, runner=runner, which=lambda _n: which, platform=platform), err


def test_runs_argument_lists_with_timeout(runner: FakeRunner) -> None:
    runner.results["ps"] = (0, "out", "")
    docker, _ = make(runner)
    result = docker.run("ps", "--format", "{{.Names}}")
    argv, kwargs = runner.calls[0]
    assert argv == ["/usr/bin/docker", "ps", "--format", "{{.Names}}"]
    assert "shell" not in kwargs
    assert kwargs["timeout"] == 60.0
    assert kwargs["capture_output"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert result.ok
    assert result.stdout == "out"


def test_input_text(runner: FakeRunner) -> None:
    docker, _ = make(runner)
    docker.run("login", input_text="secret")
    _, kwargs = runner.calls[0]
    assert kwargs["input"] == "secret"
    assert "stdin" not in kwargs


def test_compose_wrapper(runner: FakeRunner) -> None:
    docker, _ = make(runner)
    docker.compose("modelmux", Path("/cfg/compose.yaml"), "up", "-d")
    assert runner.calls[0][0][1:] == ["compose", "-p", "modelmux", "-f", "/cfg/compose.yaml",
                                      "up", "-d"]  # fmt: skip


def test_rejects_nul(runner: FakeRunner) -> None:
    docker, _ = make(runner)
    with pytest.raises(DockerError):
        docker.run("ps\x00x")
    assert runner.calls == []


@pytest.mark.parametrize(("platform", "hint"), [("linux", "Docker Engine"), ("darwin", "Desktop"),
                                                ("win32", "Desktop")])  # fmt: skip
def test_not_installed(runner: FakeRunner, platform: str, hint: str) -> None:
    docker, _ = make(runner, which=None, platform=platform)
    with pytest.raises(DockerError, match="not installed") as info:
        docker.run("ps")
    assert hint in (info.value.hint or "")
    assert info.value.exit_code == EXIT_DOCKER


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (FileNotFoundError(), "not installed"),
        (PermissionError(), "cannot be executed"),
        (subprocess.TimeoutExpired(["docker"], 1), "did not finish"),
    ],
)
def test_runner_exceptions(runner: FakeRunner, exc: BaseException, message: str) -> None:
    runner.raises = exc
    docker, _ = make(runner)
    with pytest.raises(DockerError, match=message):
        docker.run("ps")


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker "
         "daemon running?", "not running"),
        ("error during connect: open //./pipe/docker_engine: The system cannot find the file "
         "specified.", "not running"),
        ("permission denied while trying to connect to the Docker daemon socket", "not allowed"),
        ("docker: 'compose' is not a docker command.", "Compose v2"),
        ("Bind for 0.0.0.0:8101 failed: port is already allocated", "already in use"),
        ("manifest unknown", "was not found"),
        # Real Docker 29 output:
        ('Error response from daemon: failed to resolve reference "docker.io/library/python:'
         '0.0.0-nope": docker.io/library/python:0.0.0-nope: not found', "was not found"),
        ("Error response from daemon: error from registry: denied\ndenied", "refused access"),
        ("pull access denied for x, repository does not exist", "refused access"),
        ("Get \"https://ghcr.io/v2/\": dial tcp: lookup ghcr.io: no such host", "internet"),
        ("net/http: TLS handshake timeout", "internet"),
        ("no space left on device", "disk space"),
        ("something else entirely", "Docker command failed: docker pull"),
    ],
)  # fmt: skip
def test_classify(stderr: str, message: str) -> None:
    err = classify(stderr, "pull", "linux")
    assert message in err.message
    assert err.hint


def test_daemon_hint_per_platform() -> None:
    stderr = "Cannot connect to the Docker daemon"
    assert "systemctl" in (classify(stderr, "ps", "linux").hint or "")
    assert "Docker Desktop" in (classify(stderr, "ps", "darwin").hint or "")


def test_failure_hides_raw_output_by_default(runner: FakeRunner) -> None:
    runner.results["pull"] = (1, "", "weird internal error RAW-DETAIL")
    docker, err = make(runner)
    with pytest.raises(DockerError) as info:
        docker.run("pull", "x")
    assert "RAW-DETAIL" not in info.value.message
    assert "RAW-DETAIL" not in err.getvalue()


def test_verbose_shows_commands_and_output_redacted(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(redact_module, "_secrets", set())
    register_secret("server-api-key-value-123456")
    runner.results["inspect"] = (0, "token server-api-key-value-123456", "")
    docker, err = make(runner, verbose=True)
    docker.run("inspect", "a b")
    text = err.getvalue()
    assert "$ docker inspect 'a b'" in text
    assert "server-api-key-value" not in text


def test_check_without_raising(runner: FakeRunner) -> None:
    runner.results["volume"] = (1, "", "No such volume")
    docker, _ = make(runner)
    assert docker.run("volume", "inspect", "x", check=False).returncode == 1


def test_check_available(runner: FakeRunner) -> None:
    runner.results["version"] = (0, "29.8.0\n", "")
    docker, _ = make(runner)
    assert docker.check_available() == "29.8.0"
    assert runner.calls[1][0][1:] == ["compose", "version", "--short"]


def test_binary_resolved_once(runner: FakeRunner) -> None:
    lookups: list[str] = []

    def which(name: str) -> str:
        lookups.append(name)
        return "/bin/docker"

    docker = Docker(Console(io.StringIO(), io.StringIO()), runner=runner, which=which)
    docker.run("ps")
    docker.run("ps")
    assert lookups == ["docker"]
