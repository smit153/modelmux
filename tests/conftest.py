"""Shared test fixtures."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmux.config import Settings, load_settings
from modelmux.main import create_app
from tests.fakes import FAKE_ENV_KEYS, install_fake_cli

TEST_API_KEY = "test-key-" + "a" * 40
OTHER_API_KEY = "test-key-" + "b" * 40


def _fake_cli_processes() -> set[int]:
    """Live (non-zombie) processes started by the fake CLI or its children."""
    found = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ")
            state = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if state != "Z" and (b"pytest-of-" in cmdline or b"time.sleep(3600)" in cmdline):
            found.add(int(entry))
    return found


@pytest.fixture(autouse=True)
def _no_leftovers(tmp_path: Path) -> Iterator[None]:
    """After every test: no fake CLI process alive, no workspace left behind (plan 16.2)."""
    before = _fake_cli_processes()
    yield
    deadline = time.monotonic() + 5
    while (leftover := _fake_cli_processes() - before) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not leftover, f"processes left running: {sorted(leftover)}"
    work = tmp_path / "work"
    if work.is_dir():
        stale = [p.name for p in work.iterdir() if p.name.startswith("mmx-")]
        assert not stale, f"workspaces left behind: {stale}"


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
def driver_name() -> str:
    """The driver the app fixtures use. Override (e.g. parametrize) per module."""
    return "claude"


@pytest.fixture
def fake_binary(tmp_path: Path, driver_name: str) -> Path:
    """The fake CLI installed under the selected driver's binary name."""
    return install_fake_cli(tmp_path / "bin", driver_name)


@pytest.fixture
def make_settings(tmp_path: Path, driver_name: str, fake_binary: Path) -> Callable[..., Settings]:
    """Settings pointing at the fake CLI and private tmp dirs."""

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "driver": driver_name,
            "api_keys": TEST_API_KEY,
            "cli_path": fake_binary,
            "work_root": tmp_path / "work",
            "driver_home": tmp_path / "home",
            "kill_grace": 0.3,
        }
        values.update(overrides)
        return load_settings(**values)

    return factory


@pytest.fixture
def make_app(make_settings: Callable[..., Settings]) -> Callable[..., FastAPI]:
    """The real app, wired to the fake CLI (FAKE_* env passes through in tests only)."""

    def factory(**overrides: Any) -> FastAPI:
        return create_app(make_settings(**overrides), extra_env_allowlist=FAKE_ENV_KEYS)

    return factory


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    """A client for the app after its startup probe has run."""
    with TestClient(make_app(), raise_server_exceptions=False) as c:
        yield c
