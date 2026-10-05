"""``modelmux key show`` and ``modelmux config <target>``."""

from __future__ import annotations

import argparse
from pathlib import Path

from modelmux_cli import health
from modelmux_cli.clientconfig import KEY_ENV, Target, load_template, render
from modelmux_cli.console import Console
from modelmux_cli.context import make_context
from modelmux_cli.errors import CliError
from modelmux_cli.providers import get_provider
from modelmux_cli.secrets_store import ApiKey, read_api_key


def _key(home: Path) -> ApiKey:
    key = read_api_key(home)
    if key is None:
        raise CliError("ModelMux is not set up yet, so there is no API key.",
                       hint="Run: modelmux up")  # fmt: skip
    return key


def key_show(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    key = _key(ctx.home)
    console.reveal(key.value)
    console.note("Treat this like a password. Clients send it as: Authorization: Bearer <key>")
    return 0


def models_for(port: int, api_key: str) -> list[str]:
    status, body = health.get_json(port, "/v1/models", api_key=api_key)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return []
    return [m["id"] for m in body["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]


def config(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    template = load_template(args.target)
    key = _key(ctx.home)
    names = [args.provider] if args.provider else sorted(ctx.providers)
    targets = []
    for name in names:
        provider = get_provider(name)
        port = ctx.config.port(name)
        models = models_for(port, key.value)
        if not models:
            models = [provider.example_model]
            console.note(f"{provider.display_name} is not running; using its example model "
                         f"'{provider.example_model}'.")  # fmt: skip
        targets.append(Target(name, provider.display_name, f"{health.base_url(port)}/v1",
                              tuple(models)))  # fmt: skip
    text = render(template, targets, key.value if args.reveal_key else None)
    if args.reveal_key:
        console.reveal(text.rstrip("\n"))
    else:
        console.print(text.rstrip("\n"))
        if template.key_style != "env":
            console.note(f"Set {KEY_ENV} first: export {KEY_ENV}=$(modelmux key show)")
        else:
            console.note("Add the key yourself (modelmux key show), or use --reveal-key.")
    return 0
