"""Service configuration, loaded from ``MODELMUX_*`` environment variables.

Every setting is validated at startup. Validation messages name the offending
setting but never include its value, so secrets cannot leak into startup logs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict, SettingsError

MIN_API_KEY_LENGTH = 32

DRIVER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9._:-]{1,64}$")
CLI_MODEL_RE = re.compile(r"^[a-zA-Z0-9._:\-\[\]]{1,64}$")

KiB = 1024
MiB = 1024 * KiB

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
PositiveSeconds = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class ConfigError(Exception):
    """Configuration is invalid. The message is safe to print (no values)."""


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def validate_cli_model(value: str) -> str:
    """Validate a CLI model argument. Raises ``ValueError`` without echoing the value."""
    if value.startswith("-") or not CLI_MODEL_RE.fullmatch(value):
        raise ValueError(
            "CLI model values must match ^[a-zA-Z0-9._:\\-\\[\\]]{1,64}$ "
            "and must not start with '-'"
        )
    return value


def validate_model_id(value: str) -> str:
    """Validate a public model ID. Raises ``ValueError`` without echoing the value."""
    if not MODEL_ID_RE.fullmatch(value):
        raise ValueError("model IDs must match ^[a-zA-Z0-9._:-]{1,64}$")
    return value


class Settings(BaseSettings):
    """All ModelMux settings. See docs/CONFIGURATION.md."""

    model_config = SettingsConfigDict(
        env_prefix="MODELMUX_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    # Driver
    driver: str
    cli_path: Path | None = None
    driver_home: Path = Path("/home/modelmux/driver-home")
    work_root: Path = Path("/tmp/modelmux")  # noqa: S108 - per-request dirs, mode 0700
    # Build-time certification of the CLI (see modelmux.certification).
    manifest: Path = Path("/opt/modelmux/manifest.json")
    # Refuse to start unless the container runs hardened (non-root, no
    # capabilities, no-new-privileges, read-only root). Only for development.
    require_hardening: bool = True
    # Optional: restrict the models discovered from the CLI (comma-separated IDs).
    models: Annotated[tuple[str, ...] | None, NoDecode] = None

    # Inbound auth
    api_keys: Annotated[tuple[SecretStr, ...], NoDecode] = ()
    allow_no_auth: bool = False

    # HTTP
    host: str = "0.0.0.0"  # noqa: S104 - inside the container; publish to localhost only
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ()
    trusted_proxies: Annotated[tuple[str, ...], NoDecode] = ()
    enable_docs: bool = False
    enable_metrics: bool = False

    # Concurrency
    max_concurrent_processes: PositiveInt = 2
    max_queue_size: int = Field(default=16, ge=0)
    queue_timeout: PositiveSeconds = 30.0

    # Timeouts
    first_output_timeout: PositiveSeconds = 60.0
    idle_timeout: PositiveSeconds = 120.0
    total_timeout: PositiveSeconds = 600.0
    kill_grace: PositiveSeconds = 3.0

    # Output caps
    max_line_bytes: PositiveInt = 4 * MiB
    max_stdout_bytes: PositiveInt = 32 * MiB
    max_stderr_bytes: PositiveInt = 16 * KiB

    # Input limits
    max_body_bytes: PositiveInt = 2 * MiB
    max_messages: PositiveInt = 500
    max_tools: PositiveInt = 64
    max_tool_schema_bytes: PositiveInt = 64 * KiB
    max_tool_arguments_bytes: PositiveInt = 256 * KiB
    max_prompt_bytes: PositiveInt = 3 * MiB // 2
    max_stop_sequences: PositiveInt = 4

    # Logging
    log_level: LogLevel = "INFO"
    log_content: bool = False

    # ------------------------------------------------------------ validators

    @field_validator("driver")
    @classmethod
    def _check_driver(cls, value: str) -> str:
        if not DRIVER_NAME_RE.fullmatch(value):
            raise ValueError("must be a driver name matching ^[a-z][a-z0-9_-]{0,31}$")
        return value

    @field_validator("cli_path", "driver_home", "work_root", "manifest")
    @classmethod
    def _check_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @field_validator("models", mode="before")
    @classmethod
    def _parse_models(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, str) and value.lstrip().startswith("{"):
            # The 0.1 format (a JSON map) is gone: models come from the CLI now.
            raise ValueError(
                "must be a comma-separated list of model IDs the CLI offers, "
                "e.g. sonnet,haiku (JSON maps are no longer supported)"
            )
        items = _split_csv(value)
        if not items:
            raise ValueError("must list at least one model ID")
        for item in items:
            if not isinstance(item, str):
                raise ValueError("model IDs must be strings")
            validate_model_id(item)
        if len(set(items)) != len(items):
            raise ValueError("lists a model ID twice")
        return tuple(items)

    @field_validator("api_keys", mode="before")
    @classmethod
    def _parse_api_keys(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("api_keys")
    @classmethod
    def _check_api_keys(cls, value: tuple[SecretStr, ...]) -> tuple[SecretStr, ...]:
        for key in value:
            secret = key.get_secret_value()
            if len(secret) < MIN_API_KEY_LENGTH:
                raise ValueError(f"each key must be at least {MIN_API_KEY_LENGTH} characters")
            if any(ch.isspace() for ch in secret):
                raise ValueError("keys must not contain whitespace")
        return value

    @field_validator("cors_origins", "trusted_proxies", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if not self.api_keys and not self.allow_no_auth:
            raise ValueError(
                "MODELMUX_API_KEYS is required (set MODELMUX_ALLOW_NO_AUTH=true for dev only)"
            )
        if self.max_line_bytes > self.max_stdout_bytes:
            raise ValueError("MODELMUX_MAX_LINE_BYTES must not exceed MODELMUX_MAX_STDOUT_BYTES")
        if self.first_output_timeout > self.total_timeout:
            raise ValueError("MODELMUX_FIRST_OUTPUT_TIMEOUT must not exceed MODELMUX_TOTAL_TIMEOUT")
        if self.idle_timeout > self.total_timeout:
            raise ValueError("MODELMUX_IDLE_TIMEOUT must not exceed MODELMUX_TOTAL_TIMEOUT")
        return self

    # ------------------------------------------------------------ helpers

    def api_key_values(self) -> tuple[str, ...]:
        """The configured inbound keys as plain strings (for auth and redaction only)."""
        return tuple(key.get_secret_value() for key in self.api_keys)


def load_settings(**overrides: Any) -> Settings:
    """Load settings from the environment (plus overrides) or raise ``ConfigError``.

    The error message names each failing setting and why, never its value.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_input=False, include_url=False):
            loc = ".".join(str(part) for part in err["loc"])
            message = str(err["msg"]).removeprefix("Value error, ")
            problems.append(f"MODELMUX_{loc.upper()}: {message}" if loc else message)
        raise ConfigError("invalid configuration: " + "; ".join(problems)) from None
    except SettingsError:
        raise ConfigError("invalid configuration: a setting could not be parsed") from None
