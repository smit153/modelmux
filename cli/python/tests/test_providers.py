from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from modelmux_cli.errors import CliError, UsageError
from modelmux_cli.providers import get_provider, load_providers, shared_dir

SHARED = Path(__file__).resolve().parents[3] / "shared"


def schema_validator() -> Draft202012Validator:
    schema = json.loads((SHARED / "schema" / "provider.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize(
    "path", sorted((SHARED / "providers").glob("*.json")), ids=lambda p: p.name
)
def test_shared_files_match_schema(path: Path) -> None:
    errors = list(schema_validator().iter_errors(json.loads(path.read_text())))
    assert errors == []


def test_shared_dir_from_source() -> None:
    assert shared_dir().resolve() == SHARED


def test_builtin_providers() -> None:
    providers = load_providers()
    assert sorted(providers) == ["claude", "codex"]
    claude = providers["claude"]
    assert claude.driver == "claude"
    assert claude.default_port == 8101
    assert claude.volume == "claude-home"
    assert claude.home == "/home/modelmux/driver-home"
    assert claude.default_login_method == "browser"
    assert claude.login_methods["browser"].command == ("claude", "auth", "login")
    assert claude.status_command == ("claude", "auth", "status")
    assert claude.link_pattern.search("open https://example.com/x?y=1 now")
    codex = providers["codex"]
    assert codex.default_port == 8102
    assert codex.login_methods["api-key"].secret_stdin == "OpenAI API key"
    assert len({p.volume for p in providers.values()}) == len(providers)
    assert len({p.default_port for p in providers.values()}) == len(providers)


def test_get_provider() -> None:
    assert get_provider("codex").name == "codex"
    with pytest.raises(UsageError, match="Unknown provider 'nope'") as info:
        get_provider("nope")
    assert info.value.hint == "Choose one of: claude, codex."


def valid() -> dict[str, Any]:
    return json.loads((SHARED / "providers" / "claude.json").read_text())


def write(tmp_path: Path, data: dict[str, Any], name: str = "claude") -> Path:
    directory = tmp_path / "providers"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(data))
    return directory


def load(directory: Path) -> Any:
    load_providers.cache_clear()
    try:
        return load_providers(directory)
    finally:
        load_providers.cache_clear()


def mutate(change: str) -> dict[str, Any]:
    data = valid()
    match change:
        case "schema":
            data["schema_version"] = 2
        case "name":
            data["name"] = "other"
        case "volume":
            data["volume"] = "Bad Volume!"
        case "home":
            data["home"] = "relative"
        case "port":
            data["default_port"] = 80
        case "port-bool":
            data["default_port"] = True
        case "shell":
            data["login"]["methods"]["browser"]["command"] = ["sh", "-c", "rm -rf /"]
        case "empty-command":
            data["status"]["command"] = []
        case "default-method":
            data["login"]["default_method"] = "missing"
        case "regex":
            data["login"]["link_pattern"] = "("
        case "missing":
            del data["display_name"]
    return data


# Rules a JSON schema cannot express (cross-field and regex validity) are
# enforced by the loader only.
LOADER_ONLY = {"name", "default-method", "regex"}


@pytest.mark.parametrize(
    "change",
    ["schema", "name", "volume", "home", "port", "port-bool", "shell", "empty-command",
     "default-method", "regex", "missing"],
)  # fmt: skip
def test_invalid_definitions_are_rejected(tmp_path: Path, change: str) -> None:
    data = mutate(change)
    with pytest.raises(CliError, match=r"claude\.json is invalid"):
        load(write(tmp_path, data))
    if change not in LOADER_ONLY:
        # The bundled schema agrees with the strict loader.
        assert list(schema_validator().iter_errors(data))


def test_unreadable_file(tmp_path: Path) -> None:
    directory = tmp_path / "providers"
    directory.mkdir()
    (directory / "claude.json").write_text("{not json")
    with pytest.raises(CliError, match="cannot be read"):
        load(directory)


def test_no_providers(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(CliError, match="No provider definitions"):
        load(tmp_path / "empty")


def test_shared_volume_rejected(tmp_path: Path) -> None:
    other = valid()
    other["name"] = "other"
    write(tmp_path, valid())
    directory = write(tmp_path, other, "other")
    with pytest.raises(CliError, match="share a login volume"):
        load(directory)
