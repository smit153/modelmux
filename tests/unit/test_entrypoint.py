from __future__ import annotations

from typing import Any

import pytest

from modelmux import __main__ as entry
from tests.conftest import TEST_API_KEY


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setenv("MODELMUX_DRIVER", "claude")
    monkeypatch.setenv("MODELMUX_API_KEYS", TEST_API_KEY)
    return calls


def test_defaults(captured: dict[str, Any]) -> None:
    entry.main()
    assert captured == {
        "app": "modelmux.main:app",
        "host": "0.0.0.0",  # noqa: S104 - container default
        "port": 8000,
        "workers": 1,
        "server_header": False,
        "proxy_headers": False,
        "forwarded_allow_ips": None,
        "log_config": None,
        "access_log": False,
    }


def test_trusted_proxies(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_TRUSTED_PROXIES", "10.0.0.1, 10.0.0.2")
    monkeypatch.setenv("MODELMUX_PORT", "9000")
    entry.main()
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "10.0.0.1,10.0.0.2"
    assert captured["port"] == 9000


def test_bad_config_exits(
    captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODELMUX_API_KEYS", "short")
    with pytest.raises(SystemExit) as info:
        entry.main()
    assert info.value.code == 2
    assert "MODELMUX_API_KEYS" in capsys.readouterr().err
    assert "app" not in captured
