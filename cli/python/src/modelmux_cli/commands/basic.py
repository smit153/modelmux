"""``modelmux down``, ``logs`` and ``status``."""

from __future__ import annotations

import argparse

from modelmux_cli import health
from modelmux_cli.console import Console
from modelmux_cli.context import make_context
from modelmux_cli.errors import CliError
from modelmux_cli.stack import resolve_image, service_name

NOT_SET_UP = "ModelMux is not set up on this machine yet."


def down(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    if not ctx.stack.compose_file.exists():
        console.success("ModelMux is not running.")
        return 0
    ctx.stack.compose("down", timeout=120)
    console.success("Stopped ModelMux. Your logins are kept.")
    return 0


def logs(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    stack = ctx.stack
    if not stack.compose_file.exists():
        raise CliError(NOT_SET_UP, hint="Start it with: modelmux up")
    services = [service_name(ctx.providers[args.provider])] if args.provider else []
    follow = ["--follow"] if args.follow else []
    return ctx.docker.passthrough(
        "compose", "-p", "modelmux", "-f", str(stack.compose_file),
        "logs", "--no-log-prefix" if args.provider else "--timestamps",
        "--tail", str(max(args.tail, 0)), *follow, *services,
    )  # fmt: skip


def status(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    ctx.docker.check_available()
    stack = ctx.stack
    states = stack.states()
    try:
        image: str | None = resolve_image(ctx.config)
    except CliError:
        image = None

    rows = [("PROVIDER", "CONTAINER", "LOGIN", "HEALTH", "URL")]
    for name, provider in sorted(ctx.providers.items()):
        state = states.get(service_name(provider))
        port = ctx.config.port(name)
        container = state.state if state else "not started"
        login = "?" if image is None else ("yes" if stack.logged_in(provider, image) else "no")
        if state and state.state == "running":
            healthy = "ready" if health.ready(port) else "starting"
        else:
            healthy = "-"
        url = f"{health.base_url(port)}/v1" if healthy == "ready" else "-"
        rows.append((name, container, login, healthy, url))

    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        console.print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    if image is None:
        console.step("No server image chosen yet: run 'modelmux up' first.")
    for name, provider in sorted(ctx.providers.items()):
        state = states.get(service_name(provider))
        if state and state.state in {"exited", "dead"}:
            console.step(
                f"{provider.display_name} has stopped. If its login expired: modelmux login {name}"
            )
    return 0
