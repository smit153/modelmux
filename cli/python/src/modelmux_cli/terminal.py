"""Find the login link in a provider CLI's terminal output.

Output arrives in arbitrary chunks and contains escape sequences: colours
(CSI) and OSC 8 hyperlinks, which carry the URL a second time. Escapes are
removed from a rolling buffer, and a link is only reported once it is
complete, i.e. followed by whitespace, so a URL split across reads is never
opened half-finished.
"""

from __future__ import annotations

import re

_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OTHER_ESC = re.compile(rb"\x1b[@-Z\\-_]")
_BUFFER_LIMIT = 64 * 1024


def strip_escapes(data: bytes) -> str:
    data = _OSC.sub(b"", data)
    data = _CSI.sub(b"", data)
    data = _OTHER_ESC.sub(b"", data)
    return data.decode("utf-8", "replace")


class LinkScanner:
    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern = pattern
        self._raw = b""
        self.link: str | None = None

    def feed(self, data: bytes) -> str | None:
        """Add output. Returns the link the first time a complete one is seen."""
        if self.link is not None:
            return None
        self._raw = (self._raw + data)[-_BUFFER_LIMIT:]
        text = strip_escapes(self._raw)
        for match in self.pattern.finditer(text):
            end = match.end()
            if end < len(text) and text[end].isspace():
                self.link = match.group(0)
                return self.link
        return None
