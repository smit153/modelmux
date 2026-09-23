"""Structured JSON logging with per-request context.

Every line carries ``request_id``, ``driver`` and ``model`` when known. By
default nothing that could contain prompt content, completions, headers, argv
or env is logged; see ``content_logging_enabled``.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from modelmux.observability.redaction import (
    STANDARD_RECORD_ATTRS,
    RedactionFilter,
    configure_secrets,
    redact,
)

request_id_var: ContextVar[str | None] = ContextVar("modelmux_request_id", default=None)
driver_var: ContextVar[str | None] = ContextVar("modelmux_driver", default=None)
model_var: ContextVar[str | None] = ContextVar("modelmux_model", default=None)

_CONTEXT_FIELDS = (("request_id", request_id_var), ("driver", driver_var), ("model", model_var))

# uvicorn attaches an ANSI-coloured duplicate of its message.
_SKIPPED_ATTRS = STANDARD_RECORD_ATTRS | {"color_message"}

_content_logging = False


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Redacts the final line as a last safety net."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, var in _CONTEXT_FIELDS:
            value = var.get()
            if value is not None:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key not in _SKIPPED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


class StdoutHandler(logging.StreamHandler[Any]):
    """Writes to whatever ``sys.stdout`` is at emit time, not at creation time."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def setup_logging(
    level: str = "INFO",
    *,
    secrets: tuple[str, ...] = (),
    log_content: bool = False,
    stream: Any = None,
) -> None:
    """Configure the root logger. Safe to call more than once."""
    global _content_logging  # noqa: PLW0603 - process-wide logging switch
    _content_logging = log_content
    configure_secrets(secrets)

    handler: logging.Handler = (
        logging.StreamHandler(stream) if stream is not None else StdoutHandler()
    )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn through our handler; our middleware writes the access log.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True

    if log_content:
        logging.getLogger("modelmux").warning(
            "MODELMUX_LOG_CONTENT is enabled: prompts and completions will be logged "
            "(redacted). Do not use this in production.",
            extra={"event": "content_logging_enabled"},
        )


def content_logging_enabled() -> bool:
    """Whether prompt/completion content may be logged (debug only)."""
    return _content_logging
