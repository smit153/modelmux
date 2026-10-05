"""``modelmux``: set up, log in to and run ModelMux.

This module only parses arguments and reports errors. Each command lives in
``modelmux_cli.commands``; the commands that are not built yet say so.
"""

from __future__ import annotations

import argparse
import traceback
from collections.abc import Callable, Sequence

from modelmux_cli import __version__
from modelmux_cli.console import Console
from modelmux_cli.errors import EXIT_FAILURE, EXIT_INTERRUPTED, CliError
from modelmux_cli.providers import load_providers

Handler = Callable[[argparse.Namespace, Console], int]


def _not_yet(command: str) -> Handler:
    def handler(_args: argparse.Namespace, _console: Console) -> int:
        raise CliError(f"'modelmux {command}' is not available yet.", hint="Coming soon.")

    return handler


def build_parser() -> argparse.ArgumentParser:
    providers = sorted(load_providers())
    parser = argparse.ArgumentParser(
        prog="modelmux",
        description="Run AI coding CLIs as a secure, OpenAI-compatible API.",
        epilog="Start with: modelmux up, then modelmux login <provider>.",
    )
    parser.add_argument("--version", action="version", version=f"modelmux {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show details and commands")
    parser.add_argument("--no-color", action="store_true", help="disable coloured output")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    up = sub.add_parser("up", help="start ModelMux (logged-in providers)")
    up.add_argument(
        "providers", nargs="*", metavar="provider", help=f"any of: {', '.join(providers)}"
    )
    up.set_defaults(handler=_not_yet("up"))

    down = sub.add_parser("down", help="stop ModelMux (logins are kept)")
    down.set_defaults(handler=_not_yet("down"))

    logs = sub.add_parser("logs", help="show server logs")
    logs.add_argument("provider", nargs="?", choices=providers)
    logs.add_argument("-f", "--follow", action="store_true", help="keep streaming new lines")
    logs.add_argument("--tail", type=int, default=100, metavar="N", help="lines to show (100)")
    logs.set_defaults(handler=_not_yet("logs"))

    status = sub.add_parser("status", help="what is running, logged in and healthy")
    status.set_defaults(handler=_not_yet("status"))

    login = sub.add_parser("login", help="log a provider in (guided)")
    login.add_argument("provider", choices=providers)
    login.add_argument("--method", help="login method (see the provider's options)")
    login.add_argument("--no-browser", action="store_true", help="do not open a browser")
    login.add_argument("--raw", action="store_true", help="show the provider's raw output")
    login.set_defaults(handler=_not_yet("login"))

    logout = sub.add_parser("logout", help="remove a provider's saved login")
    logout.add_argument("provider", choices=providers)
    logout.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    logout.set_defaults(handler=_not_yet("logout"))

    config = sub.add_parser("config", help="print ready-to-paste client config")
    config.add_argument("target", choices=["litellm", "openai-python", "langchain", "curl", "env"])
    config.add_argument("--provider", choices=providers)
    config.add_argument("--reveal-key", action="store_true", help="include the real API key")
    config.set_defaults(handler=_not_yet("config"))

    doctor = sub.add_parser("doctor", help="diagnose common problems")
    doctor.set_defaults(handler=_not_yet("doctor"))

    upgrade = sub.add_parser("upgrade", help="run the image matching this CLI version")
    upgrade.add_argument("--image", metavar="REF", help="use a different image (advanced)")
    upgrade.set_defaults(handler=_not_yet("upgrade"))

    key = sub.add_parser("key", help="the API key clients use to call ModelMux")
    key_sub = key.add_subparsers(dest="key_command", metavar="<action>", required=True)
    key_show = key_sub.add_parser("show", help="print the API key")
    key_show.set_defaults(handler=_not_yet("key show"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    console = Console()
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        console = Console(verbose=args.verbose, color=False if args.no_color else None)
        handler: Handler = args.handler
        return handler(args, console)
    except CliError as exc:
        console.error(exc.message, exc.hint)
        return exc.exit_code
    except KeyboardInterrupt:
        console.error("Cancelled.")
        return EXIT_INTERRUPTED
    except Exception:
        console.error(
            "Something unexpected went wrong.",
            "Run again with --verbose for details, and please report it if it keeps happening.",
        )
        if console.verbose:
            console.detail(traceback.format_exc())
        return EXIT_FAILURE
