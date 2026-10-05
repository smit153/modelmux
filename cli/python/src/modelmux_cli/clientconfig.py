"""Render client config snippets from ``shared/templates/config/<target>.json``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from modelmux_cli.errors import CliError
from modelmux_cli.providers import shared_dir

KEY_ENV = "MODELMUX_API_KEY"
TARGETS = ("litellm", "openai-python", "langchain", "curl", "env")

# How each target refers to the key: (by reference, when revealed).
_KEY_STYLES: dict[str, tuple[str, Any]] = {
    "litellm": (f"os.environ/{KEY_ENV}", lambda key: key),
    "python": (f'os.environ["{KEY_ENV}"]', json.dumps),
    "shell": (f"${KEY_ENV}", lambda key: key),
    "env": ("", lambda key: key),
}
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    per: str
    key_style: str
    header: str
    entry: str
    footer: str


@dataclass(frozen=True)
class Target:
    provider: str
    display_name: str
    base_url: str
    models: tuple[str, ...]


def load_template(name: str) -> Template:
    path = shared_dir() / "templates" / "config" / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        template = Template(name=name, **{k: data[k] for k in (
            "description", "per", "key_style", "header", "entry", "footer")})  # fmt: skip
    except (OSError, ValueError, KeyError, TypeError):
        raise CliError(f"The {name!r} config template is missing or invalid.",
                       hint="Please report this bug.") from None  # fmt: skip
    if template.per not in {"provider", "model"} or template.key_style not in _KEY_STYLES:
        raise CliError(f"The {name!r} config template is invalid.", hint="Please report this bug.")
    return template


def _fill(text: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1) not in values:
            raise CliError(f"Unknown placeholder {match.group(0)} in a config template.",
                           hint="Please report this bug.")  # fmt: skip
        return values[match.group(1)]

    return _PLACEHOLDER.sub(replace, text)


def render(template: Template, targets: list[Target], api_key: str | None) -> str:
    """``api_key`` None means: refer to $MODELMUX_API_KEY instead of the value."""
    reference, literal = _KEY_STYLES[template.key_style]
    key = reference if api_key is None else literal(api_key)
    parts = [_fill(template.header, {"key": key})]
    for target in targets:
        models = target.models if template.per == "model" else target.models[:1]
        for model in models:
            parts.append(_fill(template.entry, {
                "provider": target.provider,
                "PROVIDER": target.provider.upper().replace("-", "_"),
                "provider_var": target.provider.replace("-", "_"),
                "display_name": target.display_name,
                "model": model,
                "base_url": target.base_url,
                "key": key,
            }))  # fmt: skip
    parts.append(_fill(template.footer, {"key": key}))
    return "".join(parts).rstrip("\n") + "\n"
