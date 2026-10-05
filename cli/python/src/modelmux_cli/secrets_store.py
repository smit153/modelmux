"""The API key clients use to call ModelMux (not a provider credential).

Generated once with ``secrets``, stored in ``secrets.env`` with mode 0600 in
the format compose reads as an env file. The value is registered for
redaction as soon as it is loaded, so it never appears in output unless the
user explicitly asks for it (``modelmux key show``).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from modelmux_cli.errors import CliError
from modelmux_cli.files import tighten, write_private
from modelmux_cli.paths import ensure_private_dir
from modelmux_cli.redact import register_secret

SECRETS_FILE = "secrets.env"
VARIABLE = "MODELMUX_API_KEYS"
KEY_BYTES = 32  # token_urlsafe(32) -> 43 characters; the server requires >= 32
_LINE = re.compile(rf"^{VARIABLE}=([A-Za-z0-9_-]{{32,}})$")


@dataclass(frozen=True)
class ApiKey:
    value: str
    path: Path
    created: bool = False
    permissions_fixed: bool = False


class SecretsFileError(CliError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            "The saved ModelMux API key is unreadable or damaged.",
            hint=f"Delete {path} and run 'modelmux up' to create a new key "
            "(clients using the old key will need the new one).",
        )


def read_api_key(directory: Path) -> ApiKey | None:
    path = directory / SECRETS_FILE
    if not path.exists():
        return None
    fixed = tighten(path)
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        raise SecretsFileError(path) from None
    match = _LINE.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None:
        raise SecretsFileError(path)
    value = match.group(1)
    register_secret(value)
    return ApiKey(value, path, permissions_fixed=fixed)


def ensure_api_key(directory: Path) -> ApiKey:
    """Return the saved key, creating one on first use."""
    existing = read_api_key(directory)
    if existing is not None:
        return existing
    ensure_private_dir(directory)
    value = secrets.token_urlsafe(KEY_BYTES)
    register_secret(value)
    path = directory / SECRETS_FILE
    write_private(path, f"{VARIABLE}={value}\n")
    return ApiKey(value, path, created=True)
