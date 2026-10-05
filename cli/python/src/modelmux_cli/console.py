"""Terminal output: short, friendly lines with ✓ / ✗ markers.

Colour is used only on a real terminal, never when ``NO_COLOR`` is set or
``TERM=dumb``. Symbols fall back to ASCII when the terminal encoding cannot
show them (older Windows consoles).
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from modelmux_cli.redact import redact

_RESET = "\033[0m"
_STYLES = {"ok": "\033[32m", "error": "\033[31m", "warn": "\033[33m", "dim": "\033[2m",
           "bold": "\033[1m"}  # fmt: skip
_SYMBOLS = {"ok": ("✓", "OK"), "error": ("✗", "X"), "warn": ("!", "!"), "step": ("→", ">")}


def _enable_windows_ansi() -> bool:
    """Turn on ANSI escape handling in the Windows console (Windows 10+)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes  # noqa: PLC0415 - Windows only

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def _can_encode(stream: TextIO, text: str) -> bool:
    try:
        text.encode(stream.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Console:
    def __init__(
        self,
        out: TextIO | None = None,
        err: TextIO | None = None,
        *,
        verbose: bool = False,
        color: bool | None = None,
    ) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.verbose = verbose
        if color is None:
            color = (
                self.out.isatty()
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM") != "dumb"
                and _enable_windows_ansi()
            )
        self.color = color
        self.unicode = _can_encode(self.out, "✓✗→")

    def _style(self, kind: str, text: str) -> str:
        return f"{_STYLES[kind]}{text}{_RESET}" if self.color else text

    def _symbol(self, kind: str) -> str:
        fancy, plain = _SYMBOLS[kind]
        return fancy if self.unicode else plain

    def _line(self, stream: TextIO, text: str) -> None:
        stream.write(redact(text) + "\n")
        stream.flush()

    # ------------------------------------------------------------ public API

    def print(self, text: str = "") -> None:
        """Plain output (command results, config snippets)."""
        self._line(self.out, text)

    def success(self, text: str) -> None:
        self._line(self.out, f"{self._style('ok', self._symbol('ok'))} {text}")

    def step(self, text: str) -> None:
        self._line(self.out, f"{self._style('dim', self._symbol('step'))} {text}")

    def warn(self, text: str) -> None:
        self._line(self.err, f"{self._style('warn', self._symbol('warn'))} {text}")

    def error(self, text: str, hint: str | None = None) -> None:
        self._line(self.err, f"{self._style('error', self._symbol('error'))} {text}")
        if hint:
            self._line(self.err, f"  {self._style('dim', hint)}")

    def detail(self, text: str) -> None:
        """Only shown with --verbose."""
        if self.verbose:
            self._line(self.err, self._style("dim", f"  {text}"))
