"""The CLI's settings (``config.json``): ports, and an optional image override.

Everything has a default, so the file only exists once something is changed
or ``modelmux up`` runs. A damaged file is reported, never silently replaced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modelmux_cli.errors import CliError
from modelmux_cli.files import write_private
from modelmux_cli.paths import ensure_private_dir
from modelmux_cli.providers import load_providers

CONFIG_FILE = "config.json"
CONFIG_VERSION = 1


@dataclass
class Config:
    ports: dict[str, int] = field(default_factory=dict)
    image: str | None = None  # explicit override; None means "the image pinned for this CLI"

    def port(self, provider: str) -> int:
        if provider in self.ports:
            return self.ports[provider]
        return load_providers()[provider].default_port


class ConfigFileError(CliError):
    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(
            f"Your ModelMux settings file is damaged ({problem}).",
            hint=f"Fix or delete {path} and run the command again.",
        )


def _parse(path: Path, data: Any) -> Config:
    if not isinstance(data, dict) or data.get("version") != CONFIG_VERSION:
        raise ConfigFileError(path, "unknown format version")
    ports = data.get("ports", {})
    if not isinstance(ports, dict):
        raise ConfigFileError(path, "'ports' must be an object")
    known = load_providers()
    for name, port in ports.items():
        if name not in known:
            raise ConfigFileError(path, f"unknown provider {name!r} in 'ports'")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigFileError(path, f"invalid port for {name!r}")
    image = data.get("image")
    if image is not None and not isinstance(image, str):
        raise ConfigFileError(path, "'image' must be a string")
    return Config(ports=dict(ports), image=image)


def load_config(directory: Path) -> Config:
    path = directory / CONFIG_FILE
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigFileError(path, "not valid JSON") from None
    return _parse(path, data)


def save_config(directory: Path, config: Config) -> Path:
    ensure_private_dir(directory)
    path = directory / CONFIG_FILE
    data: dict[str, Any] = {"version": CONFIG_VERSION, "ports": dict(sorted(config.ports.items()))}
    if config.image is not None:
        data["image"] = config.image
    write_private(path, json.dumps(data, indent=2) + "\n")
    return path
