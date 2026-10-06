from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmux import main
from modelmux.config import Settings
from modelmux.main import StartupError
from tests.conftest import TEST_API_KEY
from tests.fakes import FAKE_ENV_KEYS, FIXTURES
from tests.helpers import assert_openai_error

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(make_settings: Callable[..., Settings]) -> ClientFactory:
    def factory(**overrides: object) -> TestClient:
        app = main.create_app(make_settings(**overrides), extra_env_allowlist=FAKE_ENV_KEYS)
        return TestClient(app, raise_server_exceptions=False)

    return factory


def test_live(make_client: ClientFactory) -> None:
    resp = make_client().get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["x-request-id"]
    assert resp.headers["x-modelmux-driver"] == "claude"


def test_not_ready_before_startup(make_client: ClientFactory) -> None:
    resp = make_client().get("/health/ready")  # no lifespan: probe has not run
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "reason": "driver_not_ready"}


def test_ready_after_startup_probe(make_client: ClientFactory) -> None:
    with make_client() as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        client.app.state.readiness.saturated = lambda: True  # type: ignore[attr-defined]
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "saturated"


def test_readiness_tracks_limiter(make_client: ClientFactory) -> None:
    client = make_client()
    state = client.app.state  # type: ignore[attr-defined]
    assert state.readiness.saturated == state.limiter.saturated


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_disabled_by_default(make_client: ClientFactory, path: str) -> None:
    assert_openai_error(make_client().get(path), 404, "not_found", "invalid_request_error")


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_enabled(make_client: ClientFactory, path: str) -> None:
    assert make_client(enable_docs=True).get(path).status_code == 200


def test_unknown_route_is_openai_404(make_client: ClientFactory) -> None:
    resp = make_client().get("/v1/nothing")
    assert_openai_error(resp, 404, "not_found", "invalid_request_error")


def test_no_cors_by_default(make_client: ClientFactory) -> None:
    resp = make_client().get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_when_configured(make_client: ClientFactory) -> None:
    client = make_client(cors_origins=("https://app.example",))
    ok = client.get("/health/live", headers={"Origin": "https://app.example"})
    assert ok.headers["access-control-allow-origin"] == "https://app.example"
    bad = client.get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_no_server_header(make_client: ClientFactory) -> None:
    assert "server" not in make_client().get("/health/live").headers


# ------------------------------------------------------------------ fail-fast startup


def test_missing_binary(make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="does not exist"):
        main.create_app(make_settings(cli_path=tmp_path / "missing"))


def test_world_writable_binary(make_settings: Callable[..., Settings], fake_claude: Path) -> None:
    os.chmod(fake_claude, 0o777)  # noqa: S103 - deliberately unsafe
    with pytest.raises(StartupError, match="world-writable"):
        main.create_app(make_settings())


def test_unknown_driver(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(StartupError, match="unknown driver"):
        main.create_app(make_settings(driver="nosuchdriver"))


def test_models_discovered_at_startup(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.app.state.pipeline.models == {}  # type: ignore[attr-defined]
    with client:
        models = client.app.state.pipeline.models  # type: ignore[attr-defined]
        assert list(models)[:4] == ["sonnet", "opus", "haiku", "fable"]


def test_models_filter(make_client: ClientFactory) -> None:
    with make_client(models=("haiku", "claude-sonnet-5-5")) as client:
        models = client.app.state.pipeline.models  # type: ignore[attr-defined]
        assert list(models) == ["haiku", "claude-sonnet-5-5"]


def test_models_filter_unknown_stops_startup(make_client: ClientFactory) -> None:
    client = make_client(models=("gpt-6.1-sol",))
    with pytest.raises(StartupError, match="does not offer"), client:
        pass  # pragma: no cover


def test_probe_failure_stops_startup(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PROBE", "claude_fixture")
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude" / "auth_error.jsonl"))
    monkeypatch.setenv("FAKE_EXIT", "1")
    client = make_client()
    with pytest.raises(StartupError, match="live check failed: auth"), client:
        pass  # pragma: no cover


# ------------------------------------------------------------------ certification


def test_startup_skips_certified_checks(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Version and lockdown flags were certified at build time: a fake that
    # would now fail them still starts, because startup no longer runs them.
    monkeypatch.setenv("FAKE_UNKNOWN_FLAG", "--version-check-only")
    monkeypatch.setenv("FAKE_VERSION", "garbage")
    log = tmp_path / "queries.jsonl"
    monkeypatch.setenv("FAKE_MODEL_LOG", str(log))
    with make_client() as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
    assert not log.exists()  # and no /model query either


def test_missing_manifest_stops_startup(make_client: ClientFactory, tmp_path: Path) -> None:
    client = make_client(manifest=tmp_path / "none.json")
    with pytest.raises(StartupError, match="not certified"), client:
        pass  # pragma: no cover


@pytest.mark.parametrize("content", ["{", "[]", '{"schema_version": 1, "drivers": {"claude": 1}}'])
def test_broken_manifest_stops_startup(
    make_client: ClientFactory, tmp_path: Path, content: str
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(content)
    client = make_client(manifest=path)
    with pytest.raises(StartupError, match="manifest"), client:
        pass  # pragma: no cover


def test_uncertified_driver_stops_startup(
    make_client: ClientFactory, manifest: Path, tmp_path: Path
) -> None:
    data = json.loads(manifest.read_text())
    del data["drivers"]["claude"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    client = make_client(manifest=path)
    with pytest.raises(StartupError, match="does not certify driver 'claude'"), client:
        pass  # pragma: no cover


def test_changed_cli_binary_stops_startup(make_client: ClientFactory, fake_claude: Path) -> None:
    fake_claude.write_text(fake_claude.read_text() + "# changed\n")
    client = make_client()
    with pytest.raises(StartupError, match="not the certified one"), client:
        pass  # pragma: no cover


def test_changed_lockdown_stops_startup(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modelmux.drivers.claude.driver import ClaudeDriver  # noqa: PLC0415

    original = ClaudeDriver.lockdown_spec
    monkeypatch.setattr(ClaudeDriver, "lockdown_spec", lambda self: (*original(self), "--new"))
    client = make_client()
    with pytest.raises(StartupError, match="lockdown settings changed"), client:
        pass  # pragma: no cover


def test_unhardened_container_stops_startup(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "hardening_problems", lambda: ["runs as root (use ...)"])
    client = make_client(require_hardening=True)
    with pytest.raises(StartupError, match="not hardened: runs as root"), client:
        pass  # pragma: no cover


def test_hardening_check_can_be_disabled_loudly(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main, "hardening_problems", lambda: ["runs as root (use ...)"])
    with make_client(require_hardening=False) as client:
        assert client.get("/health/ready").status_code == 200
    assert '"event": "hardening_disabled"' in capsys.readouterr().out


def test_bad_work_root_stops_startup(make_client: ClientFactory, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    client = make_client(work_root=blocker)
    with pytest.raises(StartupError, match="WORK_ROOT"), client:
        pass  # pragma: no cover


# ------------------------------------------------------------------ lazy module app


def test_lazy_app_from_env(
    monkeypatch: pytest.MonkeyPatch, fake_claude: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(main, "_app", None)
    monkeypatch.setenv("MODELMUX_DRIVER", "claude")
    monkeypatch.setenv("MODELMUX_API_KEYS", TEST_API_KEY)
    monkeypatch.setenv("MODELMUX_CLI_PATH", str(fake_claude))
    monkeypatch.setenv("MODELMUX_WORK_ROOT", str(tmp_path / "w"))
    app = main.app
    assert isinstance(app, FastAPI)
    assert main.app is app


@pytest.mark.usefixtures("base_env")
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


@pytest.mark.usefixtures("base_env")
def test_lazy_app_startup_error_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(main, "_app", None)
    monkeypatch.setenv("MODELMUX_CLI_PATH", str(tmp_path / "missing"))
    with pytest.raises(SystemExit):
        _ = main.app
    assert "does not exist" in capsys.readouterr().err


def test_unknown_module_attribute() -> None:
    with pytest.raises(AttributeError):
        _ = main.nope  # type: ignore[attr-defined]
