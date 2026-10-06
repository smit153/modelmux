from __future__ import annotations

import io

import pytest

from modelmux_cli import console as console_module
from modelmux_cli import redact as redact_module
from modelmux_cli.console import Console
from modelmux_cli.redact import MASK, redact, register_secret


class Stream(io.StringIO):
    def __init__(self, encoding: str = "utf-8", tty: bool = False) -> None:
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


def make(**kwargs: object) -> tuple[Console, Stream, Stream]:
    out, err = Stream(**kwargs), Stream(**kwargs)  # type: ignore[arg-type]
    return Console(out, err), out, err


def test_messages_go_to_the_right_stream() -> None:
    console, out, err = make()
    console.success("done")
    console.step("working")
    console.print("plain")
    console.warn("careful")
    console.error("broken", "try this")
    assert out.getvalue() == "✓ done\n→ working\nplain\n"
    assert err.getvalue() == "! careful\n✗ broken\n  try this\n"


def test_no_color_when_not_a_tty() -> None:
    console, out, _ = make(tty=False)
    console.success("x")
    assert "\033[" not in out.getvalue()


def test_color_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(console_module, "_enable_windows_ansi", lambda: True)
    console, out, _ = make(tty=True)
    console.success("x")
    assert "\033[32m" in out.getvalue()


@pytest.mark.parametrize(("var", "value"), [("NO_COLOR", "1"), ("TERM", "dumb")])
def test_color_disabled_by_env(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    console, out, _ = make(tty=True)
    console.success("x")
    assert "\033[" not in out.getvalue()


def test_ascii_fallback() -> None:
    console, out, err = make(encoding="cp1252")
    console.success("done")
    console.error("bad")
    assert out.getvalue() == "OK done\n"
    assert err.getvalue() == "X bad\n"


def test_detail_only_when_verbose() -> None:
    out, err = Stream(), Stream()
    Console(out, err).detail("hidden")
    assert err.getvalue() == ""
    Console(out, err, verbose=True).detail("shown")
    assert "shown" in err.getvalue()


@pytest.fixture
def clean_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact_module, "_secrets", set())


@pytest.mark.usefixtures("clean_secrets")
def test_output_is_redacted() -> None:
    register_secret("my-generated-server-key-123456")
    console, out, err = make()
    console.print("key=my-generated-server-key-123456")
    console.error("Authorization: Bearer abc.def.ghi")
    console.detail("MODELMUX_API_KEYS=whatever")
    assert "my-generated-server-key" not in out.getvalue()
    assert MASK in out.getvalue()
    assert "abc.def" not in err.getvalue()


@pytest.mark.usefixtures("clean_secrets")
@pytest.mark.parametrize(
    "text",
    ["sk-ant-api03-abcdefghijklmnop", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_value-1",
     "MODELMUX_API_KEYS=abcdefghijklmnop1234"],
)  # fmt: skip
def test_redact_patterns(text: str) -> None:
    assert MASK in redact(f"x {text} y")


@pytest.mark.usefixtures("clean_secrets")
def test_short_values_are_not_registered() -> None:
    register_secret("abc")
    assert redact("abc") == "abc"


@pytest.mark.usefixtures("clean_secrets")
def test_hints_are_not_redacted() -> None:
    hint = "export MODELMUX_API_KEY=$(modelmux key show)"
    assert redact(hint) == hint
