"""Bundle the repository's language-neutral /shared data into the package.

From the repository, the data is at ../../shared. Inside a source
distribution it was copied to ./shared, so wheels built from the sdist (as
pip and uv do) still get it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PARTS = ("providers", "schema", "templates")


class SharedDataHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        for candidate in (root.parent.parent / "shared", root / "shared"):
            if all((candidate / part).is_dir() for part in PARTS):
                break
        else:
            raise RuntimeError("modelmux shared data (providers, schema) not found")
        prefix = "modelmux_cli/_shared" if self.target_name == "wheel" else "shared"
        for part in PARTS:
            build_data["force_include"][str(candidate / part)] = f"{prefix}/{part}"
