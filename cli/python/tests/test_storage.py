from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from modelmux_cli import redact as redact_module
from modelmux_cli.config import Config, ConfigFileError, load_config, save_config
from modelmux_cli.files import tighten, write_private
from modelmux_cli.paths import config_dir, ensure_private_dir
from modelmux_cli.redact import MASK, redact
from modelmux_cli.secrets_store import SecretsFileError, ensure_api_key, read_api_key

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
HOME = Path("/home/user")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------ paths


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", {}, HOME / ".config" / "modelmux"),
        ("linux", {"XDG_CONFIG_HOME": "/xdg"}, Path("/xdg/modelmux")),
        ("linux", {"XDG_CONFIG_HOME": "relative"}, HOME / ".config" / "modelmux"),
        ("darwin", {}, HOME / "Library" / "Application Support" / "modelmux"),
        ("win32", {"APPDATA": "/appdata"}, Path("/appdata/modelmux")),
        ("win32", {}, HOME / "AppData" / "Roaming" / "modelmux"),
        ("linux", {"MODELMUX_CLI_HOME": "/custom"}, Path("/custom")),
    ],
)
def test_config_dir(platform: str, env: dict[str, str], expected: Path) -> None:
    assert config_dir(env=env, platform=platform, home=HOME) == expected


@posix_only
def test_private_dir(tmp_path: Path) -> None:
    d = ensure_private_dir(tmp_path / "a" / "b")
    assert mode(d) == 0o700


# ------------------------------------------------------------------ files


@posix_only
def test_write_private_mode_and_atomic(tmp_path: Path) -> None:
    target = tmp_path / "f"
    write_private(target, "one")
    assert target.read_text() == "one"
    assert mode(target) == 0o600
    write_private(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]  # no temp files left


def test_write_private_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_private(tmp_path / "f", "x")
    assert list(tmp_path.iterdir()) == []


@posix_only
def test_tighten(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o644)
    assert tighten(f) is True
    assert mode(f) == 0o600
    assert tighten(f) is False


# ------------------------------------------------------------------ config


def test_config_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config == Config()
    assert config.port("claude") == 8101
    assert config.port("codex") == 8102


def test_config_round_trip(tmp_path: Path) -> None:
    path = save_config(tmp_path, Config(ports={"codex": 9000}, image="ghcr.io/x/y:1"))
    assert json.loads(path.read_text()) == {
        "version": 1, "ports": {"codex": 9000}, "image": "ghcr.io/x/y:1"
    }  # fmt: skip
    loaded = load_config(tmp_path)
    assert loaded.port("codex") == 9000
    assert loaded.port("claude") == 8101
    assert loaded.image == "ghcr.io/x/y:1"


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("{not json", "not valid JSON"),
        ('{"version": 2}', "unknown format version"),
        ('{"version": 1, "ports": []}', "'ports' must be an object"),
        ('{"version": 1, "ports": {"nope": 1}}', "unknown provider"),
        ('{"version": 1, "ports": {"claude": 0}}', "invalid port"),
        ('{"version": 1, "ports": {"claude": true}}', "invalid port"),
        ('{"version": 1, "image": 5}', "'image' must be a string"),
    ],
)
def test_damaged_config(tmp_path: Path, content: str, problem: str) -> None:
    (tmp_path / "config.json").write_text(content)
    with pytest.raises(ConfigFileError) as info:
        load_config(tmp_path)
    assert problem in info.value.message
    assert str(tmp_path / "config.json") in (info.value.hint or "")
    assert (tmp_path / "config.json").read_text() == content  # never overwritten


# ------------------------------------------------------------------ API key


@pytest.fixture(autouse=True)
def _clean_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact_module, "_secrets", set())


def test_key_created_once(tmp_path: Path) -> None:
    first = ensure_api_key(tmp_path)
    assert first.created
    assert len(first.value) >= 32
    second = ensure_api_key(tmp_path)
    assert not second.created
    assert second.value == first.value
    assert first.path.read_text() == f"MODELMUX_API_KEYS={first.value}\n"


def test_keys_are_random(tmp_path: Path) -> None:
    a = ensure_api_key(tmp_path / "a").value
    b = ensure_api_key(tmp_path / "b").value
    assert a != b


@posix_only
def test_key_file_private(tmp_path: Path) -> None:
    key = ensure_api_key(tmp_path)
    assert mode(key.path) == 0o600
    key.path.chmod(0o644)
    again = read_api_key(tmp_path)
    assert again is not None
    assert again.permissions_fixed
    assert mode(key.path) == 0o600


def test_key_is_redacted_once_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = ensure_api_key(tmp_path)
    assert redact(f"value {key.value}") == f"value {MASK}"
    monkeypatch.setattr(redact_module, "_secrets", set())
    read_api_key(tmp_path)
    assert key.value not in redact(key.value)


def test_missing_key(tmp_path: Path) -> None:
    assert read_api_key(tmp_path) is None


@pytest.mark.parametrize(
    "content",
    ["", "MODELMUX_API_KEYS=short\n", "OTHER=" + "a" * 40 + "\n",
     "MODELMUX_API_KEYS=" + "a" * 40 + "\nEXTRA=1\n", "MODELMUX_API_KEYS=has space " + "a" * 40],
)  # fmt: skip
def test_damaged_key_file(tmp_path: Path, content: str) -> None:
    (tmp_path / "secrets.env").write_text(content)
    with pytest.raises(SecretsFileError) as info:
        read_api_key(tmp_path)
    assert "a" * 40 not in info.value.message
