from __future__ import annotations

from pathlib import Path

import pytest

from modelmux.errors import InternalError
from modelmux.runtime.env import SAFE_PATH, build_env, validate_allowlist

HOME = Path("/home/modelmux/driver-home")

SOURCE = {
    "PATH": "/evil/bin:/usr/bin",
    "HOME": "/root",
    "MODELMUX_API_KEYS": "secret-inbound-key",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "LD_PRELOAD": "/tmp/x.so",
    "LANG": "en_US.UTF-8",
    "DRIVER_TOKEN": "driver-value",
}


def test_core_only() -> None:
    env = build_env(home=HOME, allowlist=frozenset(), source=SOURCE)
    assert env == {
        "PATH": SAFE_PATH,
        "HOME": str(HOME),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


def test_allowlisted_values_copied() -> None:
    env = build_env(home=HOME, allowlist=frozenset({"DRIVER_TOKEN", "ABSENT"}), source=SOURCE)
    assert env["DRIVER_TOKEN"] == "driver-value"
    assert "ABSENT" not in env
    for leaked in ("MODELMUX_API_KEYS", "AWS_SECRET_ACCESS_KEY", "LD_PRELOAD"):
        assert leaked not in env


def test_driver_env_overrides_and_must_be_allowlisted() -> None:
    env = build_env(
        home=HOME,
        allowlist=frozenset({"DRIVER_TOKEN", "DISABLE_AUTOUPDATER"}),
        driver_env={"DISABLE_AUTOUPDATER": "1", "DRIVER_TOKEN": "override"},
        source=SOURCE,
    )
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DRIVER_TOKEN"] == "override"
    with pytest.raises(InternalError):
        build_env(home=HOME, allowlist=frozenset(), driver_env={"X": "1"}, source=SOURCE)


@pytest.mark.parametrize(
    "name", ["MODELMUX_API_KEYS", "modelmux_x", "PATH", "HOME", "LANG", "1BAD", "A-B", ""]
)
def test_forbidden_allowlist_entries(name: str) -> None:
    with pytest.raises(InternalError):
        validate_allowlist(frozenset({name}))


def test_nul_rejected() -> None:
    with pytest.raises(InternalError):
        build_env(home=HOME, allowlist=frozenset({"A"}), driver_env={"A": "x\x00y"}, source={})


def test_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_API_KEYS", "k" * 40)
    monkeypatch.setenv("SOME_DRIVER_VAR", "v")
    env = build_env(home=HOME, allowlist=frozenset({"SOME_DRIVER_VAR"}))
    assert env["SOME_DRIVER_VAR"] == "v"
    assert "MODELMUX_API_KEYS" not in env
