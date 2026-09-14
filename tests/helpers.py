"""Shared test assertions."""

from __future__ import annotations

from typing import Any

import httpx


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
