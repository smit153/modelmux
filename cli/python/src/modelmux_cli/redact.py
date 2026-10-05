"""Mask secrets in anything the CLI prints (including --verbose output)."""

from __future__ import annotations

import re
from collections.abc import Iterable

MASK = "[REDACTED]"

_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 " + MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), MASK),
    (re.compile(r"(?i)\b(MODELMUX_API_KEYS?=)\S+"), r"\1" + MASK),
)

_secrets: set[str] = set()


def register_secret(*values: str) -> None:
    """Mask these exact values everywhere from now on (e.g. the server API key)."""
    _secrets.update(v for v in values if len(v) >= 8)


def registered() -> Iterable[str]:
    return frozenset(_secrets)


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
