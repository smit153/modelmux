from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from modelmux_cli import __version__, release
from modelmux_cli.errors import CliError

REPO = Path(__file__).resolve().parents[3]


def pyproject_version(path: Path) -> str:
    match = re.search(r'^version = "([^"]+)"$', path.read_text(encoding="utf-8"), re.M)
    assert match, path
    return match.group(1)


@pytest.mark.skipif(not (REPO / "server" / "pyproject.toml").exists(), reason="needs the repo")
def test_versions_are_in_sync() -> None:
    """One version for everything: the CLI, the server and the release data."""
    cli = pyproject_version(REPO / "cli" / "python" / "pyproject.toml")
    server = pyproject_version(REPO / "server" / "pyproject.toml")
    data = json.loads((REPO / "shared" / "release.json").read_text())
    assert cli == server == data["version"]
    assert __version__ in (cli, "0.0.0")


def test_development_checkout_has_no_pinned_image() -> None:
    release.release_data.cache_clear()
    assert release.pinned_image() is None


def load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    (tmp_path / "release.json").write_text(content)
    monkeypatch.setattr(release, "shared_dir", lambda: tmp_path)
    release.release_data.cache_clear()


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    release.release_data.cache_clear()


def test_pinned_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = "ghcr.io/smit153/modelmux@sha256:" + "b" * 64
    load(tmp_path, monkeypatch, json.dumps({"version": "1.2.3", "image": image}))
    assert release.pinned_image() == image


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"image": null}',
     '{"version": "1.0.0", "image": "ghcr.io/smit153/modelmux:latest"}',
     '{"version": "1.0.0", "image": 5}'],
)  # fmt: skip
def test_invalid_release_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    load(tmp_path, monkeypatch, content)
    with pytest.raises(CliError):
        release.pinned_image()
