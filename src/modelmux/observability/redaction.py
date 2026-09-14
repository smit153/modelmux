"""Secret redaction for everything that reaches the logs.

The filter is attached to the log handler, so it sees records from every
logger (ours, uvicorn's, libraries') including exception tracebacks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization headers and bearer tokens in any text.
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 " + REDACTED),
    # OpenAI / Anthropic style keys: sk-..., sk-ant-..., sk-proj-...
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    # JWT-looking strings: header.payload.signature, base64url, header starts with eyJ.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    # key=value / "key": "value" pairs with secret-looking names.
    (
        re.compile(
            r"(?i)(\"?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password"
            r"|authorization)\"?\s*[:=]\s*\"?)[^\s\",}]+"
        ),
        r"\1" + REDACTED,
    ),
)


class Redactor:
    """Masks known secret shapes plus explicitly configured secret values."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: tuple[str, ...] = ()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        # Longest first, so a secret that contains another is fully masked.
        cleaned = {s for s in secrets if s}
        self._secrets = tuple(sorted(cleaned, key=len, reverse=True))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


_redactor = Redactor()


def configure_secrets(secrets: Iterable[str]) -> None:
    """Register literal secret values (e.g. the inbound API keys) for masking."""
    _redactor.set_secrets(secrets)


def redact(text: str) -> str:
    """Redact secrets from ``text`` using the process-wide redactor."""
    return _redactor.redact(text)


class RedactionFilter(logging.Filter):
    """Rewrites every record so its message, extras and traceback are redacted.

    After this filter runs the record carries only pre-rendered, redacted
    strings: ``args`` is cleared and ``exc_info`` is converted to ``exc_text``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not break logging
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None

        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = formatter.formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)

        for key, value in list(record.__dict__.items()):
            if key not in STANDARD_RECORD_ATTRS and isinstance(value, str):
                setattr(record, key, redact(value))
        return True
