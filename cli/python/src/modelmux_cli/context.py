"""Everything a command needs, built in one place (and replaceable in tests)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from modelmux_cli.config import Config, load_config
from modelmux_cli.console import Console
from modelmux_cli.docker import Docker
from modelmux_cli.paths import config_dir
from modelmux_cli.providers import Provider, load_providers
from modelmux_cli.stack import Stack


def make_docker(console: Console) -> Docker:
    return Docker(console)


@dataclass
class Context:
    console: Console
    docker: Docker
    home: Path = field(default_factory=config_dir)

    @cached_property
    def config(self) -> Config:
        return load_config(self.home)

    @cached_property
    def providers(self) -> dict[str, Provider]:
        return load_providers()

    @cached_property
    def stack(self) -> Stack:
        return Stack(self.docker, self.home)


def make_context(console: Console) -> Context:
    return Context(console, make_docker(console))
