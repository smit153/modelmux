"""``modelmux upgrade``: run the server image that matches this CLI, keeping logins.

The image version follows the CLI version: a newer server comes with a newer
modelmux-cli. This command makes sure the image pinned for the installed CLI
(or an explicit ``--image``) is present, recreates the providers that were
running, and says when a newer CLI is available.
"""

from __future__ import annotations

import argparse

from modelmux_cli import __version__, updates
from modelmux_cli.commands import up as up_module
from modelmux_cli.config import save_config
from modelmux_cli.console import Console
from modelmux_cli.context import make_context
from modelmux_cli.stack import resolve_image, service_name


def run(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    image = resolve_image(ctx.config, args.image)
    if args.image is not None and args.image != ctx.config.image:
        ctx.config.image = args.image
        save_config(ctx.home, ctx.config)

    states = ctx.stack.states() if ctx.stack.compose_file.exists() else {}
    up_module.prepare(ctx, image)  # downloads the image if needed, rewrites compose.yaml
    console.success(f"Server image: {image}")

    running = [
        p for p in sorted(ctx.providers.values(), key=lambda p: p.name)
        if (s := states.get(service_name(p))) is not None and s.state == "running"
    ]  # fmt: skip
    if running:
        ctx.stack.compose(
            "up", "-d", *(service_name(p) for p in running), timeout=up_module.COMPOSE_UP_TIMEOUT
        )
        for provider in running:
            up_module.start_and_wait(ctx, provider)
        console.success("Upgraded. Logins were kept.")
    else:
        console.success("Nothing was running; the next 'modelmux up' uses this image.")

    latest = updates.latest_version()
    if updates.newer_available(__version__, latest):
        console.step(f"modelmux-cli {latest} is available (you have {__version__}). "
                     f"It comes with a newer server: {updates.UPGRADE_HINT}")  # fmt: skip
    return 0
