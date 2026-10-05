"""``modelmux logout <provider>``: stop it and delete its saved login."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from modelmux_cli.console import Console
from modelmux_cli.context import make_context
from modelmux_cli.errors import CliError, UsageError
from modelmux_cli.providers import get_provider
from modelmux_cli.stack import resolve_image, service_name, volume_name

# Replaceable in tests.
ask: Callable[[str], str] = input


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise UsageError(
            "Logout needs confirmation.", hint="Add --yes to confirm when not in a terminal."
        )
    return ask(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def run(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    provider = get_provider(args.provider)
    stack = ctx.stack
    if not stack.volume_exists(provider):
        console.success(f"{provider.display_name} is not logged in; nothing to remove.")
        return 0
    if not args.yes and not confirm(f"Remove the saved {provider.display_name} login?"):
        console.step("Nothing changed.")
        return 0

    if stack.compose_file.exists():
        stack.compose("rm", "--stop", "--force", service_name(provider), check=False)
    try:
        image: str | None = resolve_image(ctx.config)
    except CliError:
        image = None
    if image is not None:
        # Best effort: lets the provider revoke the token, not just forget it.
        result = ctx.docker.run(
            *stack.helper_args(provider, image, provider.logout_command), check=False, timeout=60
        )
        if not result.ok:
            console.detail("The provider's own logout did not succeed; removing the login anyway.")
    ctx.docker.run("volume", "rm", volume_name(provider))
    console.success(f"Logged out of {provider.display_name} and removed its saved login.")
    return 0
