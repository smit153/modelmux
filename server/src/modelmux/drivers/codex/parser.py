"""Parse ``codex exec --json`` lines into normalized events.

Event and item types follow codex-rs ``exec/src/exec_events.rs``
(``ThreadEvent`` / ``ThreadItemDetails``); the auth-failure stream in
tests/fixtures/codex was recorded from codex-cli 0.159.2.

Any command/file/MCP/web-search/collab item is a ``ToolAttempt`` as soon as
it is *started*, so the process is killed before the action completes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from modelmux.drivers.events import (
    Completed,
    FailureKind,
    Ignored,
    NormalizedEvent,
    ProviderFailure,
    TextFinal,
    ToolAttempt,
    UsageReport,
    looks_like_execution,
    sanitize_detail,
)

TOOL_ITEMS = frozenset(
    {"command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search"}
)
# "error" items are non-fatal notices (e.g. transport fallback); fatal errors
# arrive as turn.failed / error events.
BENIGN_ITEMS = frozenset({"reasoning", "todo_list", "error"})
RETRY_NOTICE = "Reconnecting..."

_HINTS: tuple[tuple[re.Pattern[str], FailureKind], ...] = (
    (re.compile(r"context_length_exceeded|context window|maximum context|too long", re.I),
     FailureKind.CONTEXT_LENGTH),
    (re.compile(r"\b(401|403)\b|unauthori[sz]ed|authentication|not logged in|log ?in", re.I),
     FailureKind.AUTH),
    (re.compile(r"\b429\b|rate.?limit|usage limit|quota", re.I), FailureKind.RATE_LIMITED),
    (re.compile(r"model.{0,40}(does not exist|not found|not supported)|model_not_found", re.I),
     FailureKind.MODEL_UNAVAILABLE),
    (re.compile(r"\b(500|502|503|504|529)\b|overloaded|server_error|unavailable", re.I),
     FailureKind.OVERLOADED),
)  # fmt: skip


def classify_failure(message: str) -> FailureKind:
    for pattern, kind in _HINTS:
        if pattern.search(message):
            return kind
    return FailureKind.UNKNOWN


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _item(data: dict[str, Any]) -> list[NormalizedEvent]:
    event_type = str(data["type"])
    item = data.get("item")
    if not isinstance(item, dict):
        return [ProviderFailure(FailureKind.PROTOCOL, f"{event_type} without item")]
    kind = _str(item.get("type")) or "unknown"
    if kind in TOOL_ITEMS or (kind not in BENIGN_ITEMS and looks_like_execution(kind)):
        detail = _str(item.get("tool")) or _str(item.get("server")) or ""
        return [ToolAttempt(kind=sanitize_detail(kind, 64), detail=sanitize_detail(detail))]
    if kind == "agent_message":
        if event_type == "item.completed":
            return [TextFinal(_str(item.get("text")) or "")]
        return [Ignored(f"{event_type}.agent_message")]
    return [Ignored(f"{event_type}.{sanitize_detail(kind, 48)}")]


def _turn_completed(data: dict[str, Any]) -> list[NormalizedEvent]:
    usage = data.get("usage")
    events: list[NormalizedEvent] = []
    if isinstance(usage, dict):
        # OpenAI-style input_tokens includes cached and cache-write tokens;
        # UsageReport.input_tokens is the uncached remainder.
        cached = _int(usage.get("cached_input_tokens"))
        written = _int(usage.get("cache_write_input_tokens"))
        events.append(
            UsageReport(
                input_tokens=max(0, _int(usage.get("input_tokens")) - cached - written),
                output_tokens=_int(usage.get("output_tokens")),
                cached_input_tokens=cached,
                cache_write_tokens=written,
            )
        )
    events.append(Completed(final_text=None))
    return events


def _turn_failed(data: dict[str, Any]) -> list[NormalizedEvent]:
    error = data.get("error")
    message = _str(error.get("message")) if isinstance(error, dict) else None
    message = message or "turn failed"
    return [ProviderFailure(classify_failure(message), sanitize_detail(message))]


def _error(data: dict[str, Any]) -> list[NormalizedEvent]:
    message = _str(data.get("message")) or "error"
    if message.startswith(RETRY_NOTICE):
        return [Ignored("error.retry")]
    return [ProviderFailure(classify_failure(message), sanitize_detail(message))]


_HANDLERS: dict[str, Callable[[dict[str, Any]], list[NormalizedEvent]]] = {
    "thread.started": lambda _d: [Ignored("thread.started")],
    "turn.started": lambda _d: [Ignored("turn.started")],
    "item.started": _item,
    "item.updated": _item,
    "item.completed": _item,
    "turn.completed": _turn_completed,
    "turn.failed": _turn_failed,
    "error": _error,
}


def parse_line(line: bytes) -> list[NormalizedEvent]:
    """Translate one JSONL line. Never raises."""
    if not line.strip():
        return []
    try:
        data = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return [ProviderFailure(FailureKind.PROTOCOL, "unparseable output line")]
    if not isinstance(data, dict):
        return [ProviderFailure(FailureKind.PROTOCOL, "output line is not an object")]
    event_type = _str(data.get("type"))
    if event_type is None:
        return [ProviderFailure(FailureKind.PROTOCOL, "output line without a type")]
    handler = _HANDLERS.get(event_type)
    try:
        if handler is not None:
            return handler(data)
    except Exception:  # defensive: a parser bug must not crash the request
        return [ProviderFailure(FailureKind.PROTOCOL, f"could not parse {event_type} event")]
    if looks_like_execution(event_type):
        return [ToolAttempt(kind=f"event.{sanitize_detail(event_type, 48)}", detail="")]
    return [Ignored(sanitize_detail(event_type, 64))]


# ---------------------------------------------------------------- model discovery
#
# ``codex debug models`` prints the model catalog as one JSON object (verified
# with 0.159.2). With a login it refreshes the catalog from OpenAI and writes
# it to ``$HOME/.codex/models_cache.json`` (with ``fetched_at`` and
# ``client_version``); without a login or network it silently prints the
# catalog bundled in the binary and writes no cache. The cache is how the
# driver tells a live list from a stale one.

VISIBLE = "list"


@dataclass(frozen=True)
class CatalogModel:
    slug: str
    visible: bool
    priority: int


@dataclass(frozen=True)
class CatalogCache:
    fetched_at: datetime
    client_version: str
    slugs: frozenset[str]


def _catalog_entries(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    entries: list[dict[str, Any]] = []
    for entry in data["models"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            return None
        entries.append(entry)
    return entries


def parse_model_catalog(raw: bytes) -> list[CatalogModel] | None:
    """The catalog's models, ordered by priority; None if it is not a catalog."""
    try:
        entries = _catalog_entries(json.loads(raw))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if entries is None:
        return None
    models = [
        CatalogModel(
            slug=e["slug"],
            visible=e.get("visibility") == VISIBLE and e.get("supported_in_api") is True,
            priority=e["priority"] if isinstance(e.get("priority"), int) else 1_000_000,
        )
        for e in entries
    ]
    return sorted(models, key=lambda m: m.priority)


def parse_model_cache(raw: bytes) -> CatalogCache | None:
    """The metadata of ``models_cache.json``; None if unreadable."""
    try:
        data = json.loads(raw)
        entries = _catalog_entries(data)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        version = data["client_version"]
    except (ValueError, UnicodeDecodeError, RecursionError, KeyError, TypeError):
        return None
    if entries is None or not isinstance(version, str) or fetched_at.tzinfo is None:
        return None
    return CatalogCache(fetched_at, version, frozenset(e["slug"] for e in entries))
