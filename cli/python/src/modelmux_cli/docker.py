"""The ONLY module that runs other programs: the ``docker`` CLI.

- Commands are argument lists, never shell strings.
- Every call has a timeout.
- Docker's raw output is shown only with --verbose (and redacted there);
  failures become ``DockerError``s with a plain message and a next step.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modelmux_cli.console import Console
from modelmux_cli.errors import DockerError

DEFAULT_TIMEOUT = 60.0
PULL_TIMEOUT = 1800.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Result:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _start_docker_hint(platform: str) -> str:
    if platform in ("darwin", "win32"):
        return "Start Docker Desktop and wait until it says it is running, then try again."
    return "Start the Docker service (for example: sudo systemctl start docker), then try again."


def _install_hint(platform: str) -> str:
    if platform in ("darwin", "win32"):
        return "Install Docker Desktop: https://docs.docker.com/desktop/"
    return "Install Docker Engine: https://docs.docker.com/engine/install/"


# (pattern in Docker's stderr, message, hint factory)
_CLASSIFIERS: tuple[tuple[re.Pattern[str], str, Callable[[str], str]], ...] = (
    (
        re.compile(r"permission denied while trying to connect", re.I),
        "Your user is not allowed to use Docker.",
        lambda _p: (
            "Add yourself to the docker group (sudo usermod -aG docker $USER), "
            "then log out and back in."
        ),
    ),
    (
        re.compile(
            r"cannot connect to the docker daemon|is the docker daemon running|error during "
            r"connect|docker_engine.*(cannot find|not found)|failed to connect to the docker API",
            re.I,
        ),
        "Docker is installed but not running.",
        _start_docker_hint,
    ),
    (
        re.compile(
            r"'compose' is not a docker command|unknown command.*compose|unknown shorthand"
            r" flag: 'f' in -f",
            re.I,
        ),
        "Docker Compose v2 is not available.",
        lambda _p: "Install the Docker Compose plugin: https://docs.docker.com/compose/install/",
    ),
    (
        re.compile(r"port is already allocated|address already in use|bind: .*in use", re.I),
        "A port ModelMux needs is already in use.",
        lambda _p: "Run 'modelmux doctor' to see which port, or choose another with --port.",
    ),
    (
        re.compile(
            r"manifest unknown|manifest for .* not found|not found: manifest|"
            r"failed to resolve reference .*: not found",
            re.I,
        ),
        "The ModelMux image for this version was not found.",
        lambda _p: (
            "Check for a newer modelmux-cli (pipx upgrade modelmux-cli), or run 'modelmux doctor'."
        ),
    ),
    (
        # GHCR answers a bare "denied" for images that do not exist or are private.
        re.compile(r"pull access denied|unauthorized|error from registry: denied|denied: ", re.I),
        "The image registry refused access (the image may not exist or may be private).",
        lambda _p: (
            "Check that you can reach ghcr.io and are not logged in with expired "
            "credentials (docker logout ghcr.io)."
        ),
    ),
    (
        re.compile(
            r"tls handshake timeout|i/o timeout|no such host|dial tcp|network is unreachable|"
            r"proxyconnect|connection refused|temporary failure in name resolution",
            re.I,
        ),
        "Docker could not reach the internet.",
        lambda _p: (
            "Check your connection or proxy (HTTPS_PROXY, and Docker Desktop's proxy "
            "settings), then try again."
        ),
    ),
    (
        re.compile(r"no space left on device", re.I),
        "Docker has run out of disk space.",
        lambda _p: "Free space with 'docker system prune' (it removes unused images), then retry.",
    ),
)


def classify(stderr: str, command: str, platform: str | None = None) -> DockerError:
    platform = platform or sys.platform
    for pattern, message, hint in _CLASSIFIERS:
        if pattern.search(stderr):
            return DockerError(message, hint=hint(platform))
    return DockerError(
        f"Docker command failed: docker {command}.",
        hint="Run again with --verbose to see Docker's output.",
    )


class Docker:
    def __init__(
        self,
        console: Console,
        *,
        runner: Runner | None = None,
        which: Callable[[str], str | None] = shutil.which,
        platform: str | None = None,
    ) -> None:
        self.console = console
        self._runner: Runner = runner or subprocess.run
        self._which = which
        self.platform = platform or sys.platform
        self._binary: str | None = None

    @property
    def binary(self) -> str:
        if self._binary is None:
            found = self._which("docker")
            if found is None:
                raise DockerError("Docker is not installed.", hint=_install_hint(self.platform))
            self._binary = found
        return self._binary

    def _argv(self, args: Sequence[str]) -> list[str]:
        for arg in args:
            if not isinstance(arg, str) or "\x00" in arg:
                raise DockerError("Internal error: invalid docker argument.")
        return [self.binary, *args]

    def run(
        self,
        *args: str,
        timeout: float = DEFAULT_TIMEOUT,
        input_text: str | None = None,
        check: bool = True,
    ) -> Result:
        """Run ``docker <args>`` and capture its output."""
        argv = self._argv(args)
        self.console.detail("$ docker " + shlex.join(args))
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
        }
        if input_text is not None:
            kwargs["input"] = input_text
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = self._runner(argv, **kwargs)
        except FileNotFoundError:
            raise DockerError(
                "Docker is not installed.", hint=_install_hint(self.platform)
            ) from None
        except PermissionError:
            raise DockerError(
                "The docker command cannot be executed.", hint=f"Check permissions of {argv[0]}."
            ) from None
        except subprocess.TimeoutExpired:
            raise DockerError(
                f"Docker did not finish 'docker {args[0] if args else ''}' in time.",
                hint="Docker may be busy or stuck; check Docker is healthy and try again.",
            ) from None
        result = Result(tuple(args), proc.returncode, proc.stdout or "", proc.stderr or "")
        if result.stdout.strip():
            self.console.detail(result.stdout.rstrip())
        if result.stderr.strip():
            self.console.detail(result.stderr.rstrip())
        if check and not result.ok:
            raise classify(result.stderr, args[0] if args else "", self.platform)
        return result

    def compose(self, project: str, file: Path, *args: str, **kwargs: Any) -> Result:
        return self.run("compose", "-p", project, "-f", str(file), *args, **kwargs)

    def check_available(self) -> str:
        """Docker is installed, running, and has Compose v2. Returns the server version."""
        server = self.run("version", "--format", "{{.Server.Version}}").stdout.strip()
        self.run("compose", "version", "--short")
        return server
