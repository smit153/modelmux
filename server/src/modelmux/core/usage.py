"""Map normalized usage reports to OpenAI usage."""

from __future__ import annotations

from collections.abc import Iterable

from modelmux.api.schemas import PromptTokensDetails, Usage
from modelmux.drivers.events import UsageReport


def merge_usage(reports: Iterable[UsageReport]) -> UsageReport | None:
    """Sum reports (e.g. original run plus a repair). ``None`` if there are none."""
    total: UsageReport | None = None
    for report in reports:
        if total is None:
            total = report
        else:
            total = UsageReport(
                input_tokens=total.input_tokens + report.input_tokens,
                output_tokens=total.output_tokens + report.output_tokens,
                cached_input_tokens=total.cached_input_tokens + report.cached_input_tokens,
                cache_write_tokens=total.cache_write_tokens + report.cache_write_tokens,
            )
    return total


def to_openai_usage(report: UsageReport | None) -> Usage:
    """OpenAI usage. Zeros when the driver produced none (never estimated)."""
    if report is None:
        return Usage()
    prompt = report.input_tokens + report.cached_input_tokens + report.cache_write_tokens
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=report.output_tokens,
        total_tokens=prompt + report.output_tokens,
        prompt_tokens_details=PromptTokensDetails(cached_tokens=report.cached_input_tokens),
    )
