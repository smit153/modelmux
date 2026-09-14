from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from modelmux.observability.logging import (
    content_logging_enabled,
    driver_var,
    model_var,
    request_id_var,
    setup_logging,
)
from modelmux.observability.redaction import REDACTED, Redactor

CONFIGURED_KEY = "configured-inbound-key-" + "z" * 20


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    setup_logging("DEBUG", secrets=(CONFIGURED_KEY,), stream=stream)
    yield stream
    setup_logging("INFO", stream=io.StringIO())


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz",
        "sk-proj-1234567890abcdef",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        CONFIGURED_KEY,
    ],
)
def test_redactor_masks(secret: str) -> None:
    redactor = Redactor([CONFIGURED_KEY])
    out = redactor.redact(f"before {secret} after")
    assert REDACTED in out
    assert secret not in out
    assert out.startswith("before ")
    assert out.endswith(" after")


def test_redactor_masks_key_value_pairs() -> None:
    out = Redactor().redact('{"api_key": "hunter2value", "password=swordfish"}')
    assert "hunter2value" not in out
    assert "swordfish" not in out


def test_redactor_leaves_normal_text() -> None:
    text = "request finished status=200 duration_ms=12 skipped=3"
    assert Redactor().redact(text) == text


def test_json_line_with_context(log_stream: io.StringIO) -> None:
    tokens = [request_id_var.set("req-1"), driver_var.set("claude"), model_var.set("sonnet")]
    try:
        logging.getLogger("modelmux.test").info("hello", extra={"event": "x", "count": 3})
    finally:
        for var, token in zip((request_id_var, driver_var, model_var), tokens, strict=True):
            var.reset(token)
    (line,) = _lines(log_stream)
    assert line["message"] == "hello"
    assert line["level"] == "INFO"
    assert line["request_id"] == "req-1"
    assert line["driver"] == "claude"
    assert line["model"] == "sonnet"
    assert line["event"] == "x"
    assert line["count"] == 3


def test_message_args_and_extras_redacted(log_stream: io.StringIO) -> None:
    log = logging.getLogger("modelmux.test")
    log.info("auth header %s", f"Bearer {CONFIGURED_KEY}", extra={"detail": CONFIGURED_KEY})
    raw = log_stream.getvalue()
    assert CONFIGURED_KEY not in raw
    assert REDACTED in raw


def test_exception_traceback_redacted(log_stream: io.StringIO) -> None:
    log = logging.getLogger("modelmux.test")
    try:
        raise RuntimeError(f"boom with sk-ant-secretsecretsecret and {CONFIGURED_KEY}")
    except RuntimeError:
        log.exception("failed")
    raw = log_stream.getvalue()
    assert "secretsecretsecret" not in raw
    assert CONFIGURED_KEY not in raw
    (line,) = _lines(log_stream)
    assert "RuntimeError" in str(line["exc"])


def test_third_party_loggers_redacted(log_stream: io.StringIO) -> None:
    logging.getLogger("uvicorn.error").error("oops %s", CONFIGURED_KEY)
    raw = log_stream.getvalue()
    assert CONFIGURED_KEY not in raw
    assert "oops" in raw


def test_bad_format_string_does_not_break(log_stream: io.StringIO) -> None:
    logging.getLogger("modelmux.test").info("value %d", "not-a-number")
    assert "value %d" in log_stream.getvalue()


def test_content_logging_warns() -> None:
    stream = io.StringIO()
    setup_logging("INFO", log_content=True, stream=stream)
    try:
        assert content_logging_enabled()
        assert "content_logging_enabled" in stream.getvalue()
    finally:
        setup_logging("INFO", stream=io.StringIO())
    assert not content_logging_enabled()
