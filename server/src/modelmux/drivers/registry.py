"""Driver loading and validation.

Only the driver named in ``MODELMUX_DRIVER`` is imported. Built-in drivers
are looked up here; third-party drivers come from the ``modelmux.drivers``
entry point group. Installed but unselected plugins are never loaded.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Sequence
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
    return cls(binary)


def resolve_models(
    driver_name: str, discovered: Sequence[ModelInfo], allow: Sequence[str] | None
) -> dict[str, ModelInfo]:
    """The effective allowlist: the models the CLI offers, narrowed by ``allow``.

    ``allow`` (MODELMUX_MODELS) can only restrict: naming a model the CLI did
    not offer is an error, so a typo never silently serves nothing.
    """
    result: dict[str, ModelInfo] = {}
    for model in discovered:
        try:
            validate_model_id(model.id)
            validate_cli_model(model.cli_model)
        except ValueError as exc:
            raise DriverLoadError(
                f"driver {driver_name!r} discovered an invalid model: {exc}"
            ) from None
        if model.id in result:
            raise DriverLoadError(f"driver {driver_name!r} discovered model {model.id!r} twice")
        result[model.id] = model
    if not result:
        raise DriverLoadError(f"driver {driver_name!r} discovered no models")
    if allow is None:
        return result
    unknown = [model_id for model_id in allow if model_id not in result]
    if unknown:
        raise DriverLoadError(
            f"MODELMUX_MODELS names {unknown[0]!r}, which the {driver_name} CLI does not offer"
        )
    wanted = set(allow)
    return {model_id: model for model_id, model in result.items() if model_id in wanted}
