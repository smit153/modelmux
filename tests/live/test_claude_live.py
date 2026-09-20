"""Live smoke test against the real Claude Code CLI. Skipped by default.

Run manually (makes two small sonnet requests: the startup login check and
one chat completion):

    LIVE_DRIVER_HOME=$HOME uv run pytest -m live tests/live
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modelmux.config import load_settings
from modelmux.main import create_app
from tests.conftest import TEST_API_KEY
from tests.helpers import AUTH, chat_body

pytestmark = pytest.mark.live


@pytest.fixture
def live_client(tmp_path: Path) -> TestClient:
    binary = shutil.which("claude")
    if binary is None:
        pytest.skip("claude CLI not installed")
    home = os.environ.get("LIVE_DRIVER_HOME")
    if not home:
        pytest.skip("set LIVE_DRIVER_HOME to the home that holds the Claude login")
    settings = load_settings(
        driver="claude",
        api_keys=TEST_API_KEY,
        cli_path=Path(binary),
        driver_home=Path(home),
        work_root=tmp_path / "work",
        models={"sonnet": "sonnet"},  # live tests never use other models
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_live_chat(live_client: TestClient) -> None:
    with live_client as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        resp = client.post(
            "/v1/chat/completions",
            json=chat_body("Reply with exactly the word: pineapple"),
            headers=AUTH,
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "pineapple" in data["choices"][0]["message"]["content"].lower()
    assert data["usage"]["completion_tokens"] > 0
    assert data["usage"]["prompt_tokens"] > 0
