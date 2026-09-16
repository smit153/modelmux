from __future__ import annotations

import pytest

from modelmux.core.pipeline import apply_stop, new_completion_id
from modelmux.core.usage import merge_usage, to_openai_usage
from modelmux.drivers.events import UsageReport


def test_to_openai_usage() -> None:
    usage = to_openai_usage(UsageReport(10, 5, cached_input_tokens=3, cache_write_tokens=2))
    assert usage.prompt_tokens == 15
    assert usage.completion_tokens == 5
    assert usage.total_tokens == 20
    assert usage.prompt_tokens_details.cached_tokens == 3


def test_no_usage_is_zero() -> None:
    usage = to_openai_usage(None)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (0, 0, 0)


def test_merge_usage() -> None:
    assert merge_usage([]) is None
    merged = merge_usage([UsageReport(1, 2, 3, 4), UsageReport(10, 20, 30, 40)])
    assert merged == UsageReport(11, 22, 33, 44)


@pytest.mark.parametrize(
    ("text", "stops", "expected"),
    [
        ("hello world", [], ("hello world", False)),
        ("hello world", ["xyz"], ("hello world", False)),
        ("hello world", ["o"], ("hell", True)),
        ("a END b STOP", ["STOP", "END"], ("a ", True)),
        ("STOP", ["STOP"], ("", True)),
    ],
)
def test_apply_stop(text: str, stops: list[str], expected: tuple[str, bool]) -> None:
    assert apply_stop(text, stops) == expected


def test_completion_id() -> None:
    a, b = new_completion_id(), new_completion_id()
    assert a != b
    assert a.startswith("chatcmpl-")
    assert len(a) == 33
