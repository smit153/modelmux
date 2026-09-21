"""Driver loading and validation.

Only the driver named in ``MODELMUX_DRIVER`` is imported. Built-in drivers
are looked up here; third-party drivers come from the ``modelmux.drivers``
entry point group. Installed but unselected plugins are never loaded.
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from modelmux.config import validate_cli_model, validate_model_id
from modelmux.drivers.base import Driver, ModelInfo

ENTRY_POINT_GROUP = "modelmux.drivers"

BUILTIN_DRIVERS: dict[str, str] = {
    "claude": "modelmux.drivers.claude.driver:ClaudeDriver",
    "codex": "modelmux.drivers.codex.driver:CodexDriver",
}


class DriverLoadError(Exception):
    """The selected driver is missing or invalid. The message is safe to print."""


def _import_object(target: str) -> Any:
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def load_driver_class(name: str) -> type[Driver]:
    """Import only the selected driver class and validate its declaration."""
    try:
        if name in BUILTIN_DRIVERS:
            obj = _import_object(BUILTIN_DRIVERS[name])
        else:
            matches = entry_points(group=ENTRY_POINT_GROUP, name=name)
            if not matches:
                raise DriverLoadError(f"unknown driver {name!r}")
            obj = next(iter(matches)).load()
    except DriverLoadError:
        raise
    except Exception as exc:
        raise DriverLoadError(f"driver {name!r} failed to import: {type(exc).__name__}") from exc
    validate_driver_class(obj, name)
    return obj  # type: ignore[no-any-return]


def validate_driver_class(obj: object, name: str) -> None:
    if not inspect.isclass(obj) or not issubclass(obj, Driver):
        raise DriverLoadError(f"driver {name!r} is not a Driver subclass")
    if inspect.isabstract(obj):
        raise DriverLoadError(f"driver {name!r} does not implement the full contract")
    for attr in ("name", "binary_name", "supported_versions"):
        value = getattr(obj, attr, None)
        if not isinstance(value, str) or not value:
            raise DriverLoadError(f"driver {name!r} is missing class attribute {attr}")
    if obj.name != name:
        raise DriverLoadError(f"driver {name!r} declares a different name")
    try:
        SpecifierSet(obj.supported_versions)
    except InvalidSpecifier:
        raise DriverLoadError(f"driver {name!r} has an invalid supported_versions") from None


def create_driver(cls: type[Driver], binary: Path) -> Driver:
    driver = cls(binary)
    resolve_models(driver, None)  # validates the default list
    return driver


def resolve_models(driver: Driver, override: dict[str, str] | None) -> dict[str, ModelInfo]:
    """The effective allowlist: ``override`` (MODELMUX_MODELS) or the driver default."""
    if override is not None:
        models = [ModelInfo(id=k, cli_model=v) for k, v in override.items()]
    else:
        models = driver.models()
    if not models:
        raise DriverLoadError(f"driver {driver.name!r} has no models")
    result: dict[str, ModelInfo] = {}
    for model in models:
        try:
            validate_model_id(model.id)
            validate_cli_model(model.cli_model)
        except ValueError as exc:
            raise DriverLoadError(f"driver {driver.name!r} has an invalid model: {exc}") from None
        if model.id in result:
            raise DriverLoadError(f"driver {driver.name!r} lists model {model.id!r} twice")
        result[model.id] = model
    return result
