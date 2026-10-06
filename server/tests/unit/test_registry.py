from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from modelmux.drivers import registry
from modelmux.drivers.base import Driver, ModelInfo
from modelmux.drivers.registry import (
    DriverLoadError,
    load_driver_class,
    resolve_models,
)
from tests.fakes.echo_driver import EchoDriver

BIN = Path("/usr/bin/true")


class FakeEntryPoint:
    def __init__(self, obj: object) -> None:
        self.obj = obj
        self.loaded = False

    def load(self) -> object:
        self.loaded = True
        return self.obj


@pytest.fixture
def builtin_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.BUILTIN_DRIVERS, "echo", "tests.fakes.echo_driver:EchoDriver")


@pytest.mark.usefixtures("builtin_echo")
def test_load_builtin() -> None:
    assert load_driver_class("echo") is EchoDriver


def test_load_entry_point_only_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = FakeEntryPoint(EchoDriver)
    other = FakeEntryPoint(object)
    calls: list[dict[str, Any]] = []

    def fake_entry_points(**kwargs: Any) -> list[FakeEntryPoint]:
        calls.append(kwargs)
        return [selected] if kwargs.get("name") == "echo" else [other]

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)
    assert load_driver_class("echo") is EchoDriver
    assert calls == [{"group": "modelmux.drivers", "name": "echo"}]
    assert selected.loaded
    assert not other.loaded


def test_unknown_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "entry_points", lambda **_: [])
    with pytest.raises(DriverLoadError, match="unknown driver"):
        load_driver_class("nope")


def test_import_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.BUILTIN_DRIVERS, "broken", "tests.fakes.does_not_exist:X")
    with pytest.raises(DriverLoadError, match="failed to import: ModuleNotFoundError"):
        load_driver_class("broken")


def test_unselected_builtin_not_imported() -> None:
    sys.modules.pop("modelmux.drivers.claude.driver", None)
    with pytest.raises(DriverLoadError):
        load_driver_class("nonexistent-driver")
    assert "modelmux.drivers.claude.driver" not in sys.modules


def register(monkeypatch: pytest.MonkeyPatch, name: str, obj: object) -> None:
    monkeypatch.setattr(registry, "entry_points", lambda **_: [FakeEntryPoint(obj)])


class NotADriver:
    name = "bad"


class Abstract(Driver):  # implements nothing
    name: ClassVar[str] = "abstract"
    binary_name: ClassVar[str] = "x"
    supported_versions: ClassVar[str] = ">=0"


class MissingBinary(EchoDriver):
    name: ClassVar[str] = "missing"
    binary_name: ClassVar[str] = ""


class WrongName(EchoDriver):
    name: ClassVar[str] = "other"


class BadSpec(EchoDriver):
    name: ClassVar[str] = "badspec"
    supported_versions: ClassVar[str] = "not a spec!!"


@pytest.mark.parametrize(
    ("name", "obj", "message"),
    [
        ("bad", NotADriver, "not a Driver subclass"),
        ("bad", "string", "not a Driver subclass"),
        ("abstract", Abstract, "full contract"),
        ("missing", MissingBinary, "binary_name"),
        ("wrongname", WrongName, "different name"),
        ("badspec", BadSpec, "supported_versions"),
    ],
)
def test_invalid_driver_classes(
    monkeypatch: pytest.MonkeyPatch, name: str, obj: object, message: str
) -> None:
    register(monkeypatch, name, obj)
    with pytest.raises(DriverLoadError, match=message):
        load_driver_class(name)


ECHO = ModelInfo("echo-1", "echo-1")


def test_resolve_discovered_models() -> None:
    assert resolve_models("echo", [ECHO], None) == {"echo-1": ECHO}


def test_resolve_filter_only_restricts() -> None:
    found = [ModelInfo("a", "a"), ModelInfo("b", "b"), ModelInfo("c", "c[1m]")]
    assert list(resolve_models("echo", found, ["c", "a"])) == ["a", "c"]  # discovery order
    with pytest.raises(DriverLoadError, match="names 'z', which the echo CLI does not offer"):
        resolve_models("echo", found, ["a", "z"])


@pytest.mark.parametrize(
    ("models", "message"),
    [
        ([], "discovered no models"),
        ([ModelInfo("a", "a"), ModelInfo("a", "b")], "twice"),
        ([ModelInfo("a", "--dangerous")], "invalid model"),
        ([ModelInfo("has space", "a")], "invalid model"),
    ],
)
def test_invalid_discovered_models(models: list[ModelInfo], message: str) -> None:
    with pytest.raises(DriverLoadError, match=message):
        resolve_models("echo", models, None)
