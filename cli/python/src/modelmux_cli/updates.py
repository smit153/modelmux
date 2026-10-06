"""Network checks used only by ``doctor`` and ``upgrade`` (never in the background).

Both honour the usual proxy variables (HTTPS_PROXY / NO_PROXY) via urllib.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

PYPI_URL = "https://pypi.org/pypi/modelmux-cli/json"
REGISTRY_URL = "https://ghcr.io/v2/"
UPGRADE_HINT = "pipx upgrade modelmux-cli   (or: uvx --from modelmux-cli@latest modelmux)"
_RELEASE = re.compile(r"^\d+(\.\d+)*$")


def parse_version(value: str) -> tuple[int, ...] | None:
    """Plain releases only ("1.2.3"); pre-releases and local versions are ignored."""
    return tuple(int(part) for part in value.split(".")) if _RELEASE.fullmatch(value) else None


def latest_version(timeout: float = 5.0) -> str | None:
    """The newest modelmux-cli on PyPI, or None if it cannot be checked."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    version = data.get("info", {}).get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def newer_available(current: str, latest: str | None) -> bool:
    if latest is None:
        return False
    now, new = parse_version(current), parse_version(latest)
    return now is not None and new is not None and new > now


def registry_reachable(timeout: float = 5.0) -> bool:
    """Can we reach the image registry? Any HTTP answer (even 401) counts."""
    try:
        with urllib.request.urlopen(REGISTRY_URL, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False
