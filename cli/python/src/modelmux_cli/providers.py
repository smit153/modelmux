"""Provider definitions, loaded from the shared JSON files.

The files are validated strictly here (no dependency needed); the test suite
additionally validates them against ``shared/schema/provider.schema.json``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from modelmux_cli.errors import CliError, UsageError

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VOLUME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_ARG = re.compile(r"^[A-Za-z0-9._=/-]+$")


@dataclass(frozen=True)
class LoginMethod:
    name: str
    description: str
    command: tuple[str, ...]
    secret_stdin: str | None = None


@dataclass(frozen=True)
class Provider:
    name: str
    display_name: str
    driver: str
    default_port: int
    volume: str
    home: str
    example_model: str
    login_methods: dict[str, LoginMethod]
    default_login_method: str
    link_pattern: re.Pattern[str]
    login_timeout: int
    status_command: tuple[str, ...]
    logged_in_exit_code: int
    logout_command: tuple[str, ...]
    docs_url: str | None


def shared_dir() -> Path:
    """The bundled shared data, or the repository's /shared when run from source."""
    here = Path(__file__).resolve().parent
    bundled = here / "_shared"
    if bundled.is_dir():
        return bundled
    return here.parents[3] / "shared"  # cli/python/src/modelmux_cli -> repo root


class ProviderFileError(CliError):
    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(
            f"Provider definition {path.name} is invalid: {problem}",
            hint="This is a packaging bug; please report it.",
        )


def _require(data: dict[str, Any], key: str, kind: type, path: Path) -> Any:
    value = data.get(key)
    # bool is a subclass of int; never accept true/false where a number is expected.
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ProviderFileError(path, f"'{key}' must be a {kind.__name__}")
    return value


def _command(value: Any, path: Path, where: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(a, str) and _ARG.fullmatch(a) for a in value)
    ):
        raise ProviderFileError(path, f"'{where}' must be a non-empty list of plain arguments")
    return tuple(value)


def _parse(path: Path) -> Provider:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderFileError(path, f"cannot be read ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ProviderFileError(path, "unsupported schema_version")

    name = _require(data, "name", str, path)
    if not _IDENTIFIER.fullmatch(name) or path.stem != name:
        raise ProviderFileError(path, "'name' must be an identifier matching the file name")
    volume = _require(data, "volume", str, path)
    if not _VOLUME.fullmatch(volume):
        raise ProviderFileError(path, "'volume' is not a valid volume name")
    home = _require(data, "home", str, path)
    if not home.startswith("/"):
        raise ProviderFileError(path, "'home' must be an absolute container path")
    port = _require(data, "default_port", int, path)
    if not 1024 <= port <= 65535:
        raise ProviderFileError(path, "'default_port' must be between 1024 and 65535")

    login = _require(data, "login", dict, path)
    methods: dict[str, LoginMethod] = {}
    for method_name, method in _require(login, "methods", dict, path).items():
        if not _IDENTIFIER.fullmatch(method_name) or not isinstance(method, dict):
            raise ProviderFileError(path, f"invalid login method {method_name!r}")
        secret = method.get("secret_stdin")
        methods[method_name] = LoginMethod(
            name=method_name,
            description=_require(method, "description", str, path),
            command=_command(method.get("command"), path, f"login.methods.{method_name}"),
            secret_stdin=secret if isinstance(secret, str) else None,
        )
    default_method = _require(login, "default_method", str, path)
    if default_method not in methods:
        raise ProviderFileError(path, "'login.default_method' is not one of the methods")
    try:
        link_pattern = re.compile(_require(login, "link_pattern", str, path))
    except re.error:
        raise ProviderFileError(path, "'login.link_pattern' is not a valid regex") from None

    status = _require(data, "status", dict, path)
    logout = _require(data, "logout", dict, path)
    docs_url = data.get("docs_url")
    return Provider(
        name=name,
        display_name=_require(data, "display_name", str, path),
        driver=_require(data, "driver", str, path),
        default_port=port,
        volume=volume,
        home=home,
        example_model=_require(data, "example_model", str, path),
        login_methods=methods,
        default_login_method=default_method,
        link_pattern=link_pattern,
        login_timeout=_require(login, "timeout_seconds", int, path),
        status_command=_command(status.get("command"), path, "status.command"),
        logged_in_exit_code=_require(status, "logged_in_exit_code", int, path),
        logout_command=_command(logout.get("command"), path, "logout.command"),
        docs_url=docs_url if isinstance(docs_url, str) else None,
    )


@cache
def load_providers(directory: Path | None = None) -> dict[str, Provider]:
    directory = directory or shared_dir() / "providers"
    providers = {p.name: p for p in map(_parse, sorted(directory.glob("*.json")))}
    if not providers:
        raise CliError(
            "No provider definitions found.", hint="This is a packaging bug; please report it."
        )
    volumes = [p.volume for p in providers.values()]
    if len(volumes) != len(set(volumes)):
        raise CliError("Two providers share a login volume.", hint="Please report this bug.")
    return providers


def get_provider(name: str) -> Provider:
    providers = load_providers()
    if name not in providers:
        raise UsageError(
            f"Unknown provider {name!r}.", hint=f"Choose one of: {', '.join(sorted(providers))}."
        )
    return providers[name]
