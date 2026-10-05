"""Errors the CLI reports to the user.

Every failure is a ``CliError``: a plain message saying what went wrong, an
optional hint saying what to do next, and an exit code. ``main`` is the only
place that prints them.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_DOCKER = 3
EXIT_INTERRUPTED = 130


class CliError(Exception):
    exit_code = EXIT_FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(CliError):
    """The command was used incorrectly."""

    exit_code = EXIT_USAGE


class DockerError(CliError):
    """Docker is missing, stopped, or a Docker command failed."""

    exit_code = EXIT_DOCKER
