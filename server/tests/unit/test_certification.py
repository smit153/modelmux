from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelmux import certification
from modelmux.certification import (
    Certificate,
    CertificationError,
    ManifestError,
    certify_driver,
    certify_main,
    load_certificate,
    verify_certificate,
    write_manifest,
)
from modelmux.drivers.base import ModelInfo
from modelmux.drivers.claude.driver import ClaudeDriver
from modelmux.drivers.codex.driver import CodexDriver
from modelmux.runtime.runner import resolve_binary
from tests.fakes import install_fake_cli


@pytest.fixture
def claude(tmp_path: Path) -> ClaudeDriver:
    return ClaudeDriver(resolve_binary("claude", install_fake_cli(tmp_path / "bin", "claude")))


async def test_certificate_binds_binary_and_lockdown(claude: ClaudeDriver) -> None:
    cert = await certify_driver(claude, source_env={})
    assert cert.cli_version == "2.1.285"
    assert cert.files == {"claude": certification.file_sha256(claude.binary)}
    assert cert.lockdown == certification.lockdown_sha256(claude)
    assert cert.models is not None
    assert cert.models[0] == ModelInfo("sonnet", "sonnet")
    await verify_certificate(cert, claude)  # matches itself


async def test_certify_failure_is_reported(claude: ClaudeDriver) -> None:
    with pytest.raises(CertificationError, match="failed certification: CLI does not support"):
        await certify_driver(claude, source_env={"FAKE_UNKNOWN_FLAG": "--safe-mode"})


async def test_manifest_round_trip(claude: ClaudeDriver, tmp_path: Path) -> None:
    cert = await certify_driver(claude, source_env={})
    codex = Certificate("codex", "0.159.2", {"codex": "ab" * 32}, "cd" * 32, None)
    path = tmp_path / "out" / "manifest.json"
    write_manifest(path, [cert, codex])
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert load_certificate(path, "claude") == cert
    assert load_certificate(path, "codex") == codex
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["drivers"]["codex"]["models"] is None


async def test_verify_detects_changes(
    claude: ClaudeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert = await certify_driver(claude, source_env={})
    claude.binary.write_text(claude.binary.read_text() + "#\n")
    with pytest.raises(ManifestError, match="not the certified one"):
        await verify_certificate(cert, claude)
    monkeypatch.setattr(ClaudeDriver, "lockdown_spec", lambda self: ("--other",))
    with pytest.raises(ManifestError, match="lockdown settings changed"):
        await verify_certificate(cert, claude)


async def test_verify_unreadable_binary(claude: ClaudeDriver, tmp_path: Path) -> None:
    cert = await certify_driver(claude, source_env={})
    claude.binary.unlink()
    with pytest.raises(ManifestError, match="cannot read"):
        await verify_certificate(cert, claude)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "unreadable"),
        ("[]", "malformed"),
        ('{"schema_version": 2, "drivers": {}}', "unsupported manifest schema"),
        ('{"schema_version": 1, "drivers": {}}', "does not certify driver"),
        ('{"schema_version": 1, "drivers": {"claude": {"files": {}}}}', "malformed"),
    ],
)
def test_bad_manifests(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(content)
    with pytest.raises(ManifestError, match=message):
        load_certificate(path, "claude")


def test_manifest_with_unsafe_model_is_rejected(tmp_path: Path) -> None:
    entry = {"cli_version": "1", "files": {"a": "b"}, "lockdown_sha256": "c",
             "models": [{"id": "x", "cli_model": "--help"}]}  # fmt: skip
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "drivers": {"claude": entry}}))
    with pytest.raises(ManifestError, match="malformed"):
        load_certificate(path, "claude")


def test_missing_and_oversized_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ManifestError, match="not certified"):
        load_certificate(tmp_path / "none.json", "claude")
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    monkeypatch.setattr(certification, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(ManifestError, match="too large"):
        load_certificate(path, "claude")


def test_certify_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claude = install_fake_cli(tmp_path / "bin", "claude")
    codex = install_fake_cli(tmp_path / "bin", "codex")
    out = tmp_path / "manifest.json"
    code = certify_main(["--output", str(out), "--driver", f"claude={claude}",
                         "--driver", f"codex={codex}"])  # fmt: skip
    assert code == 0
    printed = capsys.readouterr().out
    assert "certified claude 2.1.285 (8 models)" in printed
    assert "certified codex 0.159.2 (at startup)" in printed
    assert load_certificate(out, "codex").models is None
    assert CodexDriver(codex).integrity_files() == (codex,)


def test_certify_command_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "manifest.json"
    assert certify_main(["--output", str(out), "--driver", "nosuch"]) == 1
    assert "unknown driver" in capsys.readouterr().err
    assert certify_main(["--output", str(out), "--driver", f"claude={tmp_path / 'x'}"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not out.exists()
