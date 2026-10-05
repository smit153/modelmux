from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from modelmux_cli import _pinned, clientconfig, health
from modelmux_cli.clientconfig import TARGETS, Target, load_template, render
from modelmux_cli.commands import up as up_module
from modelmux_cli.errors import CliError
from modelmux_cli.main import main
from modelmux_cli.providers import shared_dir
from tests.conftest import FakeDocker

TARGETS_LIST = [
    Target("claude", "Claude Code", "http://127.0.0.1:8101/v1", ("sonnet", "opus")),
    Target("codex", "OpenAI Codex", "http://127.0.0.1:8102/v1", ("gpt-6.1-sol",)),
]
KEY = "k" * 43


# ------------------------------------------------------------------ templates


@pytest.mark.parametrize("name", TARGETS)
def test_templates_only_use_known_placeholders(name: str) -> None:
    load_template(name)  # validates fields
    raw = (shared_dir() / "templates" / "config" / f"{name}.json").read_text()
    used = set(re.findall(r"\{\{(\w+)\}\}", raw))
    assert used <= {"provider", "PROVIDER", "provider_var", "display_name", "model",
                    "base_url", "key"}  # fmt: skip


def test_litellm_lists_every_model() -> None:
    text = render(load_template("litellm"), TARGETS_LIST, None)
    assert text.startswith("model_list:\n")
    assert text.count("- model_name:") == 3
    assert "model_name: claude-opus" in text
    assert "model: openai/gpt-6.1-sol" in text
    assert "api_base: http://127.0.0.1:8102/v1" in text
    assert text.count("api_key: os.environ/MODELMUX_API_KEY") == 3


@pytest.mark.parametrize("name", ["openai-python", "langchain"])
def test_python_snippets_are_valid_python(name: str) -> None:
    for key in (None, KEY):
        source = render(load_template(name), TARGETS_LIST, key)
        ast.parse(source)
    assert 'os.environ["MODELMUX_API_KEY"]' in render(load_template(name), TARGETS_LIST, None)
    assert json.dumps(KEY) in render(load_template(name), TARGETS_LIST, KEY)


def test_langchain_disables_responses_api() -> None:
    assert "use_responses_api=False" in render(load_template("langchain"), TARGETS_LIST, None)


def test_per_provider_uses_first_model() -> None:
    text = render(load_template("openai-python"), TARGETS_LIST, None)
    assert 'model="sonnet"' in text
    assert 'model="opus"' not in text


def test_curl_and_env() -> None:
    curl = render(load_template("curl"), TARGETS_LIST, None)
    assert "Authorization: Bearer $MODELMUX_API_KEY" in curl
    assert curl.count("curl ") == 2
    env = render(load_template("env"), TARGETS_LIST, None)
    assert env.splitlines() == [
        "MODELMUX_API_KEY=",
        "MODELMUX_CLAUDE_BASE_URL=http://127.0.0.1:8101/v1",
        "MODELMUX_CLAUDE_MODEL=sonnet",
        "MODELMUX_CODEX_BASE_URL=http://127.0.0.1:8102/v1",
        "MODELMUX_CODEX_MODEL=gpt-6.1-sol",
    ]
    assert render(load_template("env"), TARGETS_LIST, KEY).startswith(f"MODELMUX_API_KEY={KEY}\n")


def test_unknown_placeholder_is_rejected() -> None:
    template = load_template("curl")
    bad = type(template)(**{**template.__dict__, "entry": "{{secret_sauce}}"})
    with pytest.raises(CliError, match="Unknown placeholder"):
        clientconfig.render(bad, TARGETS_LIST, None)


def test_missing_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(clientconfig, "shared_dir", lambda: tmp_path)
    with pytest.raises(CliError, match="missing or invalid"):
        load_template("litellm")


# ------------------------------------------------------------------ commands


@pytest.fixture
def ready_home(
    fake_docker: FakeDocker, cli_home: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Path:  # fmt: skip
    monkeypatch.setattr(_pinned, "IMAGE", "ghcr.io/smit153/modelmux@sha256:" + "a" * 64)
    monkeypatch.setattr(up_module, "port_free", lambda _port: True)
    fake_docker.when("volume", "inspect", returns=(0, "[]", ""))
    fake_docker.when("image", "inspect", returns=(0, "[]", ""))
    fake_docker.when("run", returns=(1, "", ""))  # nobody logged in
    assert main(["up"]) == 0
    capsys.readouterr()
    return cli_home


def stored_key(home: Path) -> str:
    return (home / "secrets.env").read_text().strip().split("=", 1)[1]


def fake_models(monkeypatch: pytest.MonkeyPatch, by_port: dict[int, list[str]]) -> None:
    def get_json(port: int, path: str, **_: Any) -> tuple[int, Any]:
        if port not in by_port:
            return 0, None
        return 200, {"object": "list", "data": [{"id": m} for m in by_port[port]]}

    monkeypatch.setattr(health, "get_json", get_json)


def test_key_show(ready_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["key", "show"]) == 0
    captured = capsys.readouterr()
    assert captured.out == stored_key(ready_home) + "\n"
    assert "password" in captured.err


def test_key_show_not_set_up(cli_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["key", "show"]) == 1
    assert "modelmux up" in capsys.readouterr().err


def test_config_uses_live_models_and_hides_key(
    ready_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_models(monkeypatch, {8101: ["sonnet", "opus"]})
    assert main(["config", "litellm"]) == 0
    captured = capsys.readouterr()
    assert "model_name: claude-opus" in captured.out
    assert "model: openai/gpt-6.1-sol" in captured.out  # example model for codex
    assert stored_key(ready_home) not in captured.out + captured.err
    assert "OpenAI Codex is not running" in captured.err
    assert "export MODELMUX_API_KEY=$(modelmux key show)" in captured.err
    assert "not running" not in captured.out  # notes never pollute stdout


def test_config_reveal_key(
    ready_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_models(monkeypatch, {})
    assert main(["config", "curl", "--provider", "claude", "--reveal-key"]) == 0
    out = capsys.readouterr().out
    assert f"Bearer {stored_key(ready_home)}" in out
    assert "codex" not in out.lower()


def test_config_not_set_up(cli_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "env"]) == 1
    assert "modelmux up" in capsys.readouterr().err
