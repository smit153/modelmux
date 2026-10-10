"""The release this CLI belongs to, from ``shared/release.json``.

The release workflow writes the server image pinned by digest
(``ghcr.io/smit153/modelmux@sha256:...``) into that file before building, so
every CLI implementation (Python and Node) runs the same image. In a
development checkout ``image`` is null: pass ``--image`` instead.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from modelmux_cli.errors import CliError
from modelmux_cli.providers import shared_dir


@cache
def release_data() -> dict[str, Any]:
    path = shared_dir() / "release.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CliError(
            "The release data is missing or damaged.", hint="Please report this bug."
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise CliError("The release data is invalid.", hint="Please report this bug.")
    image = data.get("image")
    if image is not None and not (isinstance(image, str) and "@sha256:" in image):
        raise CliError(
            "The pinned server image is not pinned by digest.", hint="Please report this bug."
        )
    return data


def pinned_image() -> str | None:
    image = release_data().get("image")
    return image if isinstance(image, str) else None
