"""OpenAI ``chat.completion.chunk`` SSE building and streaming stop detection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from modelmux.api.schemas import Usage
from modelmux.errors import ModelMuxError

DONE = b"data: [DONE]\n\n"


def sse(data: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(data, ensure_ascii=False).encode() + b"\n\n"


def sse_error(err: ModelMuxError) -> bytes:
    return sse(err.to_openai())


@dataclass(frozen=True)
class ChunkBuilder:
    completion_id: str
    created: int
    model: str
    fingerprint: str
    include_usage: bool

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": self.fingerprint,
            "choices": [
                {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}
            ],
        }
        if self.include_usage:
            chunk["usage"] = None
        return chunk

    def role(self) -> dict[str, Any]:
        return self._chunk({"role": "assistant", "content": ""}, None)

    def content(self, text: str) -> dict[str, Any]:
        return self._chunk({"content": text}, None)

    def finish(self, reason: str) -> dict[str, Any]:
        return self._chunk({}, reason)

    def usage(self, usage: Usage) -> dict[str, Any]:
        return {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": self.fingerprint,
            "choices": [],
            "usage": usage.model_dump(),
        }


class StopScanner:
    """Finds stop sequences in streamed text, even when split across chunks.

    Holds back the last ``len(longest stop) - 1`` characters until more text
    (or the end of the stream) shows they are not the start of a stop.
    """

    def __init__(self, stops: list[str]) -> None:
        self.stops = [s for s in stops if s]
        self.holdback = max((len(s) for s in self.stops), default=1) - 1
        self._buffer = ""
        self.stopped = False

    def feed(self, text: str) -> str:
        """Return text that is safe to emit. Sets ``stopped`` on a match."""
        if self.stopped:
            return ""
        self._buffer += text
        cut = min((i for s in self.stops if (i := self._buffer.find(s)) >= 0), default=-1)
        if cut >= 0:
            self.stopped = True
            out, self._buffer = self._buffer[:cut], ""
            return out
        safe = len(self._buffer) - self.holdback
        if safe <= 0:
            return ""
        out, self._buffer = self._buffer[:safe], self._buffer[safe:]
        return out

    def flush(self) -> str:
        out, self._buffer = self._buffer, ""
        return "" if self.stopped else out
