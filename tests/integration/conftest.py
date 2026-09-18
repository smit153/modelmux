"""Fixtures for tests that drive the full app against the fake CLI."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmux.config import Settings
from modelmux.main import create_app
from tests.conftest import TEST_API_KEY
from tests.fakes import FAKE_ENV_KEYS, FIXTURES

AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}


def chat_body(content: str = "Hi", **kwargs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "sonnet", "messages": [{"role": "user", "content": content}]}
    body.update(kwargs)
    return body


def use_claude_fixture(monkeypatch: pytest.MonkeyPatch, name: str, exit_code: int = 0) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", "claude_fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude" / name))
    monkeypatch.setenv("FAKE_EXIT", str(exit_code))


@pytest.fixture
def make_app(make_settings: Callable[..., Settings]) -> Callable[..., FastAPI]:
    def factory(**overrides: Any) -> FastAPI:
        return create_app(make_settings(**overrides), extra_env_allowlist=FAKE_ENV_KEYS)

    return factory


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    with TestClient(make_app(), raise_server_exceptions=False) as c:
        yield c
