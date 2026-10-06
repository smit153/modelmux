"""Where the CLI keeps its own files.

| OS      | Directory                                          |
|---------|----------------------------------------------------|
| Linux   | ``$XDG_CONFIG_HOME/modelmux`` (``~/.config/modelmux``) |
| macOS   | ``~/Library/Application Support/modelmux``         |
| Windows | ``%APPDATA%\\modelmux``                             |

``MODELMUX_CLI_HOME`` overrides it (tests, portable setups). The directory
holds only small files: ``config.json``, ``secrets.env`` (mode 0600) and the
generated ``compose.yaml``. Logins never live here: they stay in Docker
volumes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

APP_NAME = "modelmux"
OVERRIDE_ENV = "MODELMUX_CLI_HOME"


def config_dir(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if override := env.get(OVERRIDE_ENV):
        return Path(override).expanduser()
    home = home or Path.home()
    if platform == "win32":
        base = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg and PurePosixPath(xdg).is_absolute() else home / ".config"
    return base / APP_NAME


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` if needed; on POSIX make it accessible to the owner only."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)
    return path
