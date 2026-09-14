"""Construction of the CLI subprocess environment.

The environment is always built from scratch, never copied from
``os.environ``. Only the core minimal set plus a driver's explicit allowlist
is passed, and ``MODELMUX_*`` variables can never be passed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from modelmux.errors import InternalError

SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
CORE_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
LOCALE_DEFAULTS: Mapping[str, str] = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
FORBIDDEN_PREFIX = "MODELMUX_"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_allowlist(allowlist: frozenset[str]) -> frozenset[str]:
    """Reject allowlists that could leak ModelMux secrets or override core keys."""
    for name in allowlist:
        if not _NAME_RE.fullmatch(name):
            raise InternalError(f"invalid env allowlist entry {name!r}")
        if name.upper().startswith(FORBIDDEN_PREFIX):
            raise InternalError(f"env allowlist may not contain {FORBIDDEN_PREFIX}* ({name})")
        if name in CORE_KEYS:
            raise InternalError(f"env allowlist may not override core key {name}")
    return allowlist


def build_env(
    *,
    home: Path,
    allowlist: frozenset[str],
    driver_env: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess environment.

    ``allowlist`` names may be copied from ``source`` (ModelMux's own
    environment by default). ``driver_env`` values set by the driver must also
    be allowlisted and override ``source``.
    """
    validate_allowlist(allowlist)
    source = os.environ if source is None else source

    env: dict[str, str] = {"PATH": SAFE_PATH, "HOME": str(home)}
    for key, default in LOCALE_DEFAULTS.items():
        env[key] = source.get(key) or default

    for name in sorted(allowlist):
        value = source.get(name)
        if value is not None:
            env[name] = value

    for name, value in (driver_env or {}).items():
        if name not in allowlist:
            raise InternalError(f"driver env key {name} is not in the driver allowlist")
        env[name] = value

    for name, value in env.items():
        if "\x00" in name or "\x00" in value:
            raise InternalError(f"env value for {name} contains NUL")
    return env
