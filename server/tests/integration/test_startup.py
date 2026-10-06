"""Real uvicorn process: fail-fast startup and a healthy start."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import TEST_API_KEY


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(cli: Path, tmp_path: Path, manifest: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "MODELMUX_DRIVER": "claude",
        "MODELMUX_API_KEYS": TEST_API_KEY,
        "MODELMUX_CLI_PATH": str(cli),
        "MODELMUX_WORK_ROOT": str(tmp_path / "work"),
        "MODELMUX_DRIVER_HOME": str(tmp_path / "home"),
        "MODELMUX_MANIFEST": str(manifest),
        "MODELMUX_REQUIRE_HARDENING": "false",  # this test process is not a container
    }


def start(env: dict[str, str], port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "modelmux.main:app", "--workers", "1",
         "--no-server-header", "--no-proxy-headers", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )  # fmt: skip


def test_healthy_start(fake_claude: Path, tmp_path: Path, manifest: Path) -> None:
    port = free_port()
    proc = start(server_env(fake_claude, tmp_path, manifest), port)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=1)
                if resp.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:  # pragma: no cover
            pytest.fail("server did not become ready")
        assert resp.json() == {"status": "ready"}
    finally:
        proc.terminate()
        proc.wait(10)


def test_uncertified_cli_exits_non_zero(tmp_path: Path, manifest: Path) -> None:
    other = tmp_path / "bin" / "claude"
    other.parent.mkdir()
    other.write_text(f"#!{sys.executable}\nprint('2.1.285 (Claude Code)')\n")
    os.chmod(other, 0o700)
    proc = start(server_env(other, tmp_path, manifest), free_port())
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0
    text = output.decode()
    assert "not the certified one" in text
    assert TEST_API_KEY not in text


def test_unhardened_process_exits_non_zero(
    fake_claude: Path, tmp_path: Path, manifest: Path
) -> None:
    # The real check against this (unhardened) test process.
    env = server_env(fake_claude, tmp_path, manifest)
    del env["MODELMUX_REQUIRE_HARDENING"]
    proc = start(env, free_port())
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0
    assert b"the container is not hardened" in output


def test_missing_cli_exits_non_zero(tmp_path: Path, manifest: Path) -> None:
    proc = start(server_env(tmp_path / "nope", tmp_path, manifest), free_port())
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0
    assert b"does not exist" in output
