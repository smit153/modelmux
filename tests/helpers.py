"""Shared test assertions."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import TEST_API_KEY
from tests.fakes import FIXTURES

AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}


def chat_body(content: str = "Hi", **kwargs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "sonnet", "messages": [{"role": "user", "content": content}]}
    body.update(kwargs)
    return body


def use_claude_fixture(monkeypatch: pytest.MonkeyPatch, name: str, exit_code: int = 0) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", "claude_fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude" / name))
    monkeypatch.setenv("FAKE_EXIT", str(exit_code))


def assert_openai_error(
    resp: httpx.Response,
    status: int,
    code: str,
    otype: str,
    *,
    forbidden: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assert ``resp`` is an OpenAI-shaped error and return its ``error`` object."""
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["x-request-id"]
    body = resp.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert set(err) == {"message", "type", "code", "param"}
    assert err["code"] == code
    assert err["type"] == otype
    for text in forbidden:
        assert text not in resp.text
    return dict(err)
