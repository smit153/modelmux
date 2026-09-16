"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modelmux.config import Settings, load_settings
from tests.fakes import install_fake_cli

TEST_API_KEY = "test-key-" + "a" * 40
OTHER_API_KEY = "test-key-" + "b" * 40


@pytest.fixture(autouse=True)
def _clean_modelmux_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test start without any MODELMUX_* variables from the host."""
    for name in list(os.environ):
        if name.startswith("MODELMUX_"):
            monkeypatch.delenv(name)


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimal valid environment."""
    monkeypatch.setenv("MODELMUX_DRIVER", "claude")
    monkeypatch.setenv("MODELMUX_API_KEYS", TEST_API_KEY)


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """The fake CLI installed under the name ``claude``."""
    return install_fake_cli(tmp_path / "bin", "claude")


@pytest.fixture
def make_settings(tmp_path: Path, fake_claude: Path) -> Callable[..., Settings]:
    """Settings pointing at the fake CLI and private tmp dirs."""

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "driver": "claude",
            "api_keys": TEST_API_KEY,
            "cli_path": fake_claude,
            "work_root": tmp_path / "work",
            "driver_home": tmp_path / "home",
            "kill_grace": 0.3,
        }
        values.update(overrides)
        return load_settings(**values)

    return factory
