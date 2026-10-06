from __future__ import annotations

from pathlib import Path

import pytest

from modelmux.config import ConfigError, Settings, load_settings
from tests.conftest import OTHER_API_KEY, TEST_API_KEY

pytestmark = pytest.mark.usefixtures("base_env")


def test_defaults_load() -> None:
    settings = load_settings()
    assert settings.driver == "claude"
    assert settings.api_key_values() == (TEST_API_KEY,)
    assert settings.work_root == Path("/tmp/modelmux")
    assert settings.max_concurrent_processes == 2
    assert settings.max_queue_size == 16
    assert settings.max_body_bytes == 2 * 1024 * 1024
    assert settings.max_prompt_bytes == 1572864
    assert settings.enable_docs is False
    assert settings.models is None


def test_driver_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODELMUX_DRIVER")
    with pytest.raises(ConfigError, match="MODELMUX_DRIVER"):
        load_settings()


@pytest.mark.parametrize("name", ["Claude", "-x", "a b", "../x", ""])
def test_driver_name_validated(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("MODELMUX_DRIVER", name)
    with pytest.raises(ConfigError, match="MODELMUX_DRIVER"):
        load_settings()


def test_api_keys_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODELMUX_API_KEYS")
    with pytest.raises(ConfigError, match="MODELMUX_API_KEYS is required"):
        load_settings()


def test_allow_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODELMUX_API_KEYS")
    monkeypatch.setenv("MODELMUX_ALLOW_NO_AUTH", "true")
    assert load_settings().api_keys == ()


def test_multiple_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_API_KEYS", f" {TEST_API_KEY} , {OTHER_API_KEY},")
    assert load_settings().api_key_values() == (TEST_API_KEY, OTHER_API_KEY)


def test_short_api_key_rejected_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_API_KEYS", f"{TEST_API_KEY},short-secret-value")
    with pytest.raises(ConfigError) as info:
        load_settings()
    message = str(info.value)
    assert "MODELMUX_API_KEYS" in message
    assert "short-secret-value" not in message
    assert TEST_API_KEY not in message


def test_api_keys_hidden_in_repr() -> None:
    settings = load_settings()
    assert TEST_API_KEY not in repr(settings)
    assert TEST_API_KEY not in str(settings.model_dump())


@pytest.mark.parametrize("var", ["MODELMUX_WORK_ROOT", "MODELMUX_DRIVER_HOME", "MODELMUX_CLI_PATH"])
def test_paths_must_be_absolute(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setenv(var, "relative/path")
    with pytest.raises(ConfigError, match=var):
        load_settings()


def test_models_filter_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_MODELS", " sonnet, claude-haiku-4-5-20251001 ,")
    assert load_settings().models == ("sonnet", "claude-haiku-4-5-20251001")


def test_models_old_json_map_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_MODELS", '{"sonnet": "sonnet"}')
    with pytest.raises(ConfigError, match="JSON maps are no longer supported"):
        load_settings()


@pytest.mark.parametrize(
    "raw",
    [",", " , ", "has space", "x;rm", "bad id!", "sonnet[1m]", "a,a", "x" * 65],
)
def test_models_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("MODELMUX_MODELS", raw)
    with pytest.raises(ConfigError, match="MODELMUX_MODELS") as info:
        load_settings()
    assert "--help" not in str(info.value)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("MODELMUX_MAX_CONCURRENT_PROCESSES", "0"),
        ("MODELMUX_TOTAL_TIMEOUT", "-1"),
        ("MODELMUX_PORT", "70000"),
        ("MODELMUX_MAX_BODY_BYTES", "0"),
        ("MODELMUX_LOG_LEVEL", "LOUD"),
        ("MODELMUX_MAX_QUEUE_SIZE", "-1"),
    ],
)
def test_invalid_numbers(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match=var):
        load_settings()


def test_non_numeric_value_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_IDLE_TIMEOUT", "soon")
    with pytest.raises(ConfigError, match="MODELMUX_IDLE_TIMEOUT") as info:
        load_settings()
    assert "soon" not in str(info.value)


def test_log_level_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_LOG_LEVEL", "debug")
    assert load_settings().log_level == "DEBUG"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MODELMUX_MAX_LINE_BYTES": "100", "MODELMUX_MAX_STDOUT_BYTES": "10"}, "MAX_LINE_BYTES"),
        ({"MODELMUX_FIRST_OUTPUT_TIMEOUT": "10", "MODELMUX_TOTAL_TIMEOUT": "5"}, "FIRST_OUTPUT"),
        ({"MODELMUX_IDLE_TIMEOUT": "100", "MODELMUX_TOTAL_TIMEOUT": "80"}, "IDLE_TIMEOUT"),
    ],
)
def test_cross_field_checks(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match=message):
        load_settings()


def test_csv_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELMUX_CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("MODELMUX_TRUSTED_PROXIES", "10.0.0.1")
    settings = load_settings()
    assert settings.cors_origins == ("https://a.example", "https://b.example")
    assert settings.trusted_proxies == ("10.0.0.1",)


def test_settings_are_frozen() -> None:
    settings = load_settings()
    with pytest.raises(Exception, match="frozen"):
        settings.port = 1  # type: ignore[misc]


def test_overrides() -> None:
    assert load_settings(port=9000).port == 9000
    assert isinstance(load_settings(), Settings)
