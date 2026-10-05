from __future__ import annotations

import json

import pytest

from modelmux.api.schemas import Usage
from modelmux.core.streaming import DONE, ChunkBuilder, StopScanner, sse, sse_error
from modelmux.errors import ProviderTimeoutError


def builder(include_usage: bool = False) -> ChunkBuilder:
    return ChunkBuilder("chatcmpl-x", 1, "sonnet", "fp", include_usage)


def test_chunks() -> None:
    b = builder()
    role = b.role()
    assert role["object"] == "chat.completion.chunk"
    assert role["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert role["choices"][0]["finish_reason"] is None
    assert "usage" not in role
    assert b.content("hi")["choices"][0]["delta"] == {"content": "hi"}
    finish = b.finish("stop")
    assert finish["choices"][0] == {
        "index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"
    }  # fmt: skip


def test_include_usage() -> None:
    b = builder(include_usage=True)
    assert b.content("x")["usage"] is None
    usage = b.usage(Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5))
    assert usage["choices"] == []
    assert usage["usage"]["total_tokens"] == 5


def test_sse_encoding() -> None:
    assert sse({"a": "é"}) == 'data: {"a": "é"}\n\n'.encode()
    assert DONE == b"data: [DONE]\n\n"
    body = json.loads(sse_error(ProviderTimeoutError("secret detail"))[6:])
    assert body["error"]["code"] == "provider_timeout"
    assert "secret" not in json.dumps(body)


def run(stops: list[str], pieces: list[str]) -> tuple[str, bool]:
    scanner = StopScanner(stops)
    out = "".join(scanner.feed(p) for p in pieces)
    return out + scanner.flush(), scanner.stopped


@pytest.mark.parametrize(
    ("stops", "pieces", "expected"),
    [
        ([], ["hel", "lo"], ("hello", False)),
        (["END"], ["hello"], ("hello", False)),
        (["END"], ["hello E", "N", "D more"], ("hello ", True)),
        (["END"], ["hello EN", "X"], ("hello ENX", False)),
        (["END", "STOP"], ["a S", "TOP b END"], ("a ", True)),
        (["x"], ["abc", "dxe"], ("abcd", True)),
        (["END"], ["END"], ("", True)),
        (["END"], ["ok EN"], ("ok EN", False)),  # partial match flushed at the end
    ],
)
def test_stop_scanner(stops: list[str], pieces: list[str], expected: tuple[str, bool]) -> None:
    assert run(stops, pieces) == expected


def test_holdback_emits_early() -> None:
    scanner = StopScanner(["STOP"])
    assert scanner.feed("abcdef") == "abc"  # last 3 chars held back
    assert scanner.feed("gh") == "de"
    assert scanner.feed("xx") == "fg"
    assert scanner.flush() == "hxx"


def test_feed_after_stop_is_ignored() -> None:
    scanner = StopScanner(["!"])
    assert scanner.feed("hi!") == "hi"
    assert scanner.feed("more") == ""
    assert scanner.flush() == ""
