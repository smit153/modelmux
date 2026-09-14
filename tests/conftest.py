"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

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
