"""The reference docs must cover every setting and every error code."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from modelmux import errors
from modelmux.config import Settings

DOCS = Path(__file__).resolve().parents[3] / "docs"


def error_codes() -> list[str]:
    return sorted(
        cls.code
        for _, cls in inspect.getmembers(errors, inspect.isclass)
        if issubclass(cls, errors.ModelMuxError) and "code" in cls.__dict__
    )


@pytest.mark.parametrize("name", sorted(Settings.model_fields))
def test_every_setting_is_documented(name: str) -> None:
    assert f"`MODELMUX_{name.upper()}`" in (DOCS / "CONFIGURATION.md").read_text()


@pytest.mark.parametrize("code", error_codes())
def test_every_error_code_is_documented(code: str) -> None:
    assert f"| `{code}` |" in (DOCS / "ERRORS.md").read_text()


def test_documented_settings_exist() -> None:
    text = (DOCS / "CONFIGURATION.md").read_text()
    documented = {
        line.split("`")[1].removeprefix("MODELMUX_").lower()
        for line in text.splitlines()
        if line.startswith("| `MODELMUX_")
    }
    assert documented <= set(Settings.model_fields)
