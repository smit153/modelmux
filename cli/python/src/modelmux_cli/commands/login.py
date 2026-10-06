"""``modelmux login <provider>``: guided login, then a real test.

The provider's own login command runs in a short-lived, hardened helper
container with only that provider's login volume. On Linux and macOS it runs
on a pseudo-terminal so the login link can be spotted and opened in the
browser while the user still types or pastes codes directly into the CLI.
On Windows (or with ``--raw``) the user's console is attached directly.

Keystrokes, pasted codes and API keys are never stored or logged.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import secrets
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator

from modelmux_cli import health
from modelmux_cli.commands import up as up_module
from modelmux_cli.console import Console
from modelmux_cli.context import Context, make_context
from modelmux_cli.errors import CliError, DockerError, UsageError
from modelmux_cli.providers import LoginMethod, Provider, get_provider
from modelmux_cli.redact import register_secret
from modelmux_cli.stack import resolve_image, service_name
from modelmux_cli.terminal import LinkScanner

SECRET_LOGIN_TIMEOUT = 120.0

# Replaceable in tests.
open_browser: Callable[[str], bool] = webbrowser.open
read_secret: Callable[[str], str] = getpass.getpass


def use_pty(args: argparse.Namespace) -> bool:
    return os.name == "posix" and not args.raw


def choose_method(provider: Provider, requested: str | None) -> LoginMethod:
    name = requested or provider.default_login_method
    if name not in provider.login_methods:
        options = ", ".join(f"{m.name} ({m.description})" for m in provider.login_methods.values())
        raise UsageError(
            f"{provider.display_name} has no login method {name!r}.", hint=f"Choose: {options}"
        )
    return provider.login_methods[name]


def ensure_running_and_tested(ctx: Context, provider: Provider) -> None:
    """(Re)start the provider and wait for /health/ready: the server's startup
    check makes one tiny real request, so ready means the login works."""
    port = ctx.config.port(provider.name)
    state = ctx.stack.states().get(service_name(provider))
    if state is not None and state.state == "running" and health.ready(port):
        ctx.console.success(f"{provider.display_name} is running at {health.base_url(port)}/v1")
    else:
        up_module.check_ports(ctx, [provider])
        ctx.stack.compose(
            "up", "-d", "--force-recreate", service_name(provider),
            timeout=up_module.COMPOSE_UP_TIMEOUT,
        )  # fmt: skip
        up_module.start_and_wait(ctx, provider)
    ctx.console.success(f"{provider.display_name} is logged in and tested.")
    ctx.console.step("Configure your tools: modelmux config litellm (or openai-python, langchain)")


def _link_handler(
    provider: Provider, console: Console, no_browser: bool
) -> Callable[[bytes], bytes | None]:
    scanner = LinkScanner(provider.link_pattern)

    def on_output(data: bytes) -> bytes | None:
        link = scanner.feed(data)
        if link is None:
            return None
        if no_browser:
            return b"\r\n-> Open the link above in your browser.\r\n"
        opened = False
        try:
            opened = open_browser(link)
        except (webbrowser.Error, OSError):  # no usable browser here
            opened = False
        if opened:
            return b"\r\n-> Opened your browser. If it did not open, use the link above.\r\n"
        return b"\r\n-> Could not open a browser here: open the link above yourself.\r\n"

    return on_output


@contextlib.contextmanager
def cancel_on_signals() -> Iterator[None]:
    """Treat SIGTERM and SIGHUP (terminal closed) like Ctrl+C while logging in,
    so the helper container is always removed and the user sees "Cancelled."."""
    if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _interactive_login(
    ctx: Context, provider: Provider, method: LoginMethod, image: str, args: argparse.Namespace
) -> int:
    name = f"modelmux-login-{provider.name}-{secrets.token_hex(4)}"
    run_args = ctx.stack.helper_args(provider, image, method.command, ("-it", "--name", name))
    try:
        with cancel_on_signals():
            if use_pty(args):
                return ctx.docker.run_pty(
                    *run_args,
                    on_output=_link_handler(provider, ctx.console, args.no_browser),
                    timeout=provider.login_timeout,
                )
            ctx.console.step(
                "Follow the instructions below; open the link they show in your browser."
            )
            return ctx.docker.passthrough(*run_args, timeout=provider.login_timeout)
    except (TimeoutError, DockerError) as exc:
        _remove(ctx, name)
        if isinstance(exc, DockerError) and "in time" not in exc.message:
            raise
        raise CliError(
            "The login timed out.", hint=f"Start again: modelmux login {provider.name}"
        ) from None
    except KeyboardInterrupt:
        _remove(ctx, name)
        raise


def _secret_login(ctx: Context, provider: Provider, method: LoginMethod, image: str) -> int:
    prompt = method.secret_stdin or "Secret"
    secret = read_secret(f"{prompt} (input is hidden): ").strip()
    if not secret:
        raise UsageError(f"No {prompt} entered.")
    register_secret(secret)
    run_args = ctx.stack.helper_args(provider, image, method.command, ("-i",))
    return ctx.docker.run(
        *run_args, input_text=secret + "\n", check=False, timeout=SECRET_LOGIN_TIMEOUT
    ).returncode


def _remove(ctx: Context, container: str) -> None:
    ctx.docker.run("rm", "-f", container, check=False)


def run(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    provider = get_provider(args.provider)
    method = choose_method(provider, args.method)
    image = resolve_image(ctx.config)
    up_module.prepare(ctx, image)
    stack = ctx.stack

    if not args.force and stack.logged_in(provider, image):
        console.success(
            f"{provider.display_name} is already logged in (use --force to log in again)."
        )
        ensure_running_and_tested(ctx, provider)
        return 0

    console.step(f"Logging in to {provider.display_name}: {method.description}.")
    if method.secret_stdin:
        _secret_login(ctx, provider, method, image)
    else:
        _interactive_login(ctx, provider, method, image, args)
    console.print()
    if not stack.logged_in(provider, image):
        raise CliError(
            f"The {provider.display_name} login did not complete.",
            hint=f"Try again: modelmux login {provider.name} (add --raw to see only the "
            "provider's own output).",
        )
    console.success(f"Logged in to {provider.display_name}.")
    ensure_running_and_tested(ctx, provider)
    return 0
