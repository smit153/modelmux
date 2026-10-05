from __future__ import annotations

import io
import os
import re
import sys
import time
from pathlib import Path

import pytest

from modelmux_cli.console import Console
from modelmux_cli.docker import Docker
from modelmux_cli.providers import load_providers
from modelmux_cli.terminal import LinkScanner, strip_escapes

FIXTURES = Path(__file__).parent / "fixtures" / "login"
CLAUDE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-"
    "5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth"
    "%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3A"
    "sessions%3Aclaude_code+user%3Amcp_servers+user%3Afile_upload+user%3Aplugins&code_challenge"
    "=FAKECHALLENGE00000000000000000000000000000&code_challenge_method=S256&state="
    "FAKESTATE0000000000000000000000000000000"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason="pseudo-terminals are POSIX-only")


def feed_in_chunks(scanner: LinkScanner, data: bytes, size: int) -> list[str]:
    found = []
    for i in range(0, len(data), size):
        if (link := scanner.feed(data[i : i + size])) is not None:
            found.append(link)
    return found


@pytest.mark.parametrize("size", [1, 7, 64, 4096])
def test_claude_link_from_recording(size: int) -> None:
    provider = load_providers()["claude"]
    data = (FIXTURES / "claude_browser.raw").read_bytes()
    assert feed_in_chunks(LinkScanner(provider.link_pattern), data, size) == [CLAUDE_URL]


@pytest.mark.parametrize("size", [1, 5, 4096])
def test_codex_link_from_recording(size: int) -> None:
    provider = load_providers()["codex"]
    data = (FIXTURES / "codex_device.raw").read_bytes()
    found = feed_in_chunks(LinkScanner(provider.link_pattern), data, size)
    assert found == ["https://auth.openai.com/codex/device"]


def test_strip_escapes() -> None:
    raw = (FIXTURES / "claude_browser.raw").read_bytes()
    text = strip_escapes(raw)
    assert "\x1b" not in text
    assert "\x07" not in text
    assert text.count("https://") == 1  # the OSC 8 copy is gone
    assert "Paste code here if prompted >" in text
    codex = strip_escapes((FIXTURES / "codex_device.raw").read_bytes())
    assert "ABCD-12345" in codex


def test_incomplete_link_is_not_reported() -> None:
    scanner = LinkScanner(re.compile(r"https://\S+"))
    assert scanner.feed(b"visit https://example.com/long/pa") is None
    assert scanner.feed(b"th?x=1") is None
    assert scanner.feed(b"\r\n") == "https://example.com/long/path?x=1"
    assert scanner.feed(b"https://other.example.com/ ") is None  # first link only


def test_no_link() -> None:
    assert LinkScanner(re.compile(r"https://\S+")).feed(b"no links here\n") is None


# ------------------------------------------------------------------ the pty relay

FAKE_DOCKER = """#!{python}
import os, sys
sys.stdout.buffer.write(open(os.environ["FAKE_FIXTURE"], "rb").read())
sys.stdout.flush()
if os.environ.get("FAKE_HANG"):
    import time; time.sleep(60)
line = sys.stdin.readline()
print("\\r\\ngot:" + line.strip() + ":")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture
def fake_docker_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "docker"
    path.write_text(FAKE_DOCKER.format(python=sys.executable))
    path.chmod(0o700)
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex_device.raw"))
    return path


def make_docker(path: Path) -> Docker:
    return Docker(Console(io.StringIO(), io.StringIO()), which=lambda _n: str(path))


@posix_only
def test_relay_output_input_and_exit_code(
    fake_docker_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "3")
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(in_w, b"secret-pasted-code\n")
    seen: list[bytes] = []

    def on_output(data: bytes) -> bytes | None:
        seen.append(data)
        return b"[EXTRA]" if len(seen) == 1 else None

    code = make_docker(fake_docker_bin).run_pty(
        "run", "x", on_output=on_output, timeout=10, stdin_fd=in_r, stdout_fd=out_w
    )
    os.close(out_w)
    shown = b""
    while chunk := os.read(out_r, 65536):
        shown += chunk
    assert code == 3
    assert b"auth.openai.com/codex/device" in shown
    assert b"[EXTRA]" in shown
    assert b"got:secret-pasted-code:" in shown
    assert b"".join(seen) in shown.replace(b"[EXTRA]", b"")


@posix_only
def test_relay_timeout_kills_the_program(
    fake_docker_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HANG", "1")
    in_r, _in_w = os.pipe()
    _out_r, out_w = os.pipe()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        make_docker(fake_docker_bin).run_pty(
            "run", on_output=lambda _d: None, timeout=1, stdin_fd=in_r, stdout_fd=out_w
        )
    assert time.monotonic() - started < 5
