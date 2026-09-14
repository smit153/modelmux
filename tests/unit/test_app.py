from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmux import main
from modelmux.config import load_settings
from tests.conftest import TEST_API_KEY
from tests.helpers import assert_openai_error

pytestmark = pytest.mark.usefixtures("base_env")


def make_client(**overrides: object) -> TestClient:
    return TestClient(main.create_app(load_settings(**overrides)), raise_server_exceptions=False)


def test_live() -> None:
    resp = make_client().get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["x-request-id"]
    assert resp.headers["x-modelmux-driver"] == "claude"


def test_ready_without_driver() -> None:
    resp = make_client().get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "reason": "driver_not_ready"}


def test_ready_transitions() -> None:
    client = make_client()
    readiness = client.app.state.readiness  # type: ignore[attr-defined]
    readiness.driver_ready = True
    assert client.get("/health/ready").json() == {"status": "ready"}
    readiness.saturated = lambda: True
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "saturated"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_disabled_by_default(path: str) -> None:
    assert_openai_error(make_client().get(path), 404, "not_found", "invalid_request_error")


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_enabled(path: str) -> None:
    assert make_client(enable_docs=True).get(path).status_code == 200


def test_unknown_route_is_openai_404() -> None:
    assert_openai_error(make_client().get("/v1/nothing"), 404, "not_found", "invalid_request_error")


def test_no_cors_by_default() -> None:
    resp = make_client().get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_when_configured() -> None:
    client = make_client(cors_origins=("https://app.example",))
    ok = client.get("/health/live", headers={"Origin": "https://app.example"})
    assert ok.headers["access-control-allow-origin"] == "https://app.example"
    bad = client.get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_no_server_header() -> None:
    assert "server" not in make_client().get("/health/live").headers


def test_lazy_app_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_app", None)
    app = main.app
    assert isinstance(app, FastAPI)
    assert main.app is app


def test_lazy_app_bad_config_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main, "_app", None)
    monkeypatch.setenv("MODELMUX_API_KEYS", f"{TEST_API_KEY},tooshort")
    with pytest.raises(SystemExit) as info:
        _ = main.app
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "MODELMUX_API_KEYS" in err
    assert "tooshort" not in err
    assert TEST_API_KEY not in err


def test_unknown_module_attribute() -> None:
    with pytest.raises(AttributeError):
        _ = main.nope  # type: ignore[attr-defined]
