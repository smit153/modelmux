"""Startup self-check of the container's hardening (no processes, Linux /proc only).

The image's own facts are certified at build time; how the container was
*started* can only be checked at runtime. ``modelmux up`` and the compose
example set all of these.
"""

from __future__ import annotations

import os
from pathlib import Path

PROC_STATUS = Path("/proc/self/status")


def _status_field(status: str, name: str) -> str | None:
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    return None


def hardening_problems(
    *,
    euid: int | None = None,
    status: str | None = None,
    root_read_only: bool | None = None,
) -> list[str]:
    """What is missing from the expected hardening, as short fixes. Empty if all is set."""
    euid = os.geteuid() if euid is None else euid
    if status is None:
        try:
            status = PROC_STATUS.read_text()
        except OSError:
            status = ""
    if root_read_only is None:
        root_read_only = bool(os.statvfs("/").f_flag & os.ST_RDONLY)
    problems = []
    if euid == 0:
        problems.append("runs as root (use a non-root user, e.g. --user 10001:10001)")
    caps = _status_field(status, "CapEff")
    if caps is None or int(caps, 16) != 0:
        problems.append("has Linux capabilities (use --cap-drop ALL)")
    if _status_field(status, "NoNewPrivs") != "1":
        problems.append("can gain privileges (use --security-opt no-new-privileges)")
    if not root_read_only:
        problems.append("root filesystem is writable (use --read-only)")
    return problems
