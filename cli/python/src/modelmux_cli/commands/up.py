"""``modelmux up``: prepare everything and start the logged-in providers."""

from __future__ import annotations

import argparse

from modelmux_cli import health
from modelmux_cli.config import save_config
from modelmux_cli.console import Console
from modelmux_cli.context import Context, make_context
from modelmux_cli.errors import CliError, UsageError
from modelmux_cli.paths import ensure_private_dir
from modelmux_cli.ports import port_free
from modelmux_cli.providers import Provider
from modelmux_cli.secrets_store import ensure_api_key
from modelmux_cli.stack import build_compose, resolve_image, service_name, write_compose

READY_TIMEOUT = 240.0
POLL_INTERVAL = 2.0
COMPOSE_UP_TIMEOUT = 300.0


def parse_port_overrides(values: list[str], known: set[str]) -> dict[str, int]:
    ports: dict[str, int] = {}
    for value in values:
        name, sep, raw = value.partition("=")
        if not sep or name not in known or not raw.isdigit() or not 1024 <= int(raw) <= 65535:
            raise UsageError(
                f"Invalid --port {value!r}.",
                hint="Use --port <provider>=<1024-65535>, e.g. claude=9101.",
            )
        ports[name] = int(raw)
    return ports


def apply_settings(ctx: Context, args: argparse.Namespace) -> str:
    """Validate and remember --image / --port; return the image to run."""
    config = ctx.config
    image = resolve_image(config, args.image)
    ports = parse_port_overrides(args.port or [], set(ctx.providers))
    changed = bool(ports) or (args.image is not None and args.image != config.image)
    config.ports.update(ports)
    if args.image is not None:
        config.image = args.image
    used = [config.port(name) for name in ctx.providers]
    if len(used) != len(set(used)):
        raise UsageError(
            "Two providers are set to the same port.", hint="Choose different --port values."
        )
    if changed:
        save_config(ctx.home, config)
    return image


def start_and_wait(ctx: Context, provider: Provider) -> None:
    console, stack = ctx.console, ctx.stack
    service = service_name(provider)
    port = ctx.config.port(provider.name)

    def stopped() -> bool:
        state = stack.states().get(service)
        return state is not None and state.state in {"exited", "dead"}

    console.step(f"Starting {provider.display_name} (it checks its login, about 10-30 s)...")
    if not health.wait_until(
        lambda: health.ready(port), timeout=READY_TIMEOUT, interval=POLL_INTERVAL,
        should_stop=stopped,
    ):  # fmt: skip
        if stopped():
            raise CliError(
                f"{provider.display_name} stopped while starting.",
                hint=f"Its login may have expired: modelmux login {provider.name}. "
                f"Details: modelmux logs {provider.name}",
            )
        raise CliError(
            f"{provider.display_name} did not become ready in time.",
            hint=f"See what it is doing: modelmux logs {provider.name}",
        )
    console.success(f"{provider.display_name} is running at {health.base_url(port)}/v1")


def prepare(ctx: Context, image: str) -> None:
    """Docker, API key, compose file, image and login volumes: all idempotent."""
    console, stack = ctx.console, ctx.stack
    console.step("Checking Docker...")
    ctx.docker.check_available()
    ensure_private_dir(ctx.home)
    key = ensure_api_key(ctx.home)
    if key.created:
        console.success("Created an API key for your clients (see it with: modelmux key show).")
    write_compose(ctx.home, build_compose(ctx.providers, ctx.config, image, key.path))
    if not stack.image_present(image):
        console.step(f"Downloading the ModelMux server image {image} (first time only, ~1.4 GB)...")
        stack.ensure_image(image)
    for provider in ctx.providers.values():
        stack.ensure_volume(provider)


def check_ports(ctx: Context, providers: list[Provider]) -> None:
    states = ctx.stack.states()
    for provider in providers:
        running = states.get(service_name(provider))
        port = ctx.config.port(provider.name)
        if (running is None or running.state != "running") and not port_free(port):
            raise CliError(
                f"Port {port} for {provider.display_name} is already in use by another program.",
                hint=f"Pick another port: modelmux up --port {provider.name}=<port>, "
                "or run 'modelmux doctor'.",
            )


def run(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    providers = ctx.providers
    unknown = [name for name in args.providers if name not in providers]
    if unknown:
        raise UsageError(
            f"Unknown provider {unknown[0]!r}.",
            hint=f"Choose from: {', '.join(sorted(providers))}.",
        )
    image = apply_settings(ctx, args)
    prepare(ctx, image)

    targets = [providers[name] for name in (args.providers or sorted(providers))]
    console.step("Checking logins...")
    logged_in = {p.name: ctx.stack.logged_in(p, image) for p in targets}
    missing = [p for p in targets if not logged_in[p.name]]
    if args.providers and missing:
        raise CliError(
            f"{missing[0].display_name} is not logged in yet.",
            hint=f"Log in first: modelmux login {missing[0].name}",
        )
    to_start = [p for p in targets if logged_in[p.name]]
    if not to_start:
        console.success("ModelMux is set up. No provider is logged in yet.")
        console.step(f"Next: modelmux login {targets[0].name}")
        return 0

    check_ports(ctx, to_start)
    states = ctx.stack.states()
    was_running = {
        p.name for p in to_start
        if (s := states.get(service_name(p))) is not None and s.state == "running"
    }  # fmt: skip
    ctx.stack.compose("up", "-d", *(service_name(p) for p in to_start), timeout=COMPOSE_UP_TIMEOUT)
    for provider in to_start:
        port = ctx.config.port(provider.name)
        if provider.name in was_running and health.ready(port):
            console.success(
                f"{provider.display_name} is already running at {health.base_url(port)}/v1"
            )
        else:
            start_and_wait(ctx, provider)
    for provider in missing:
        console.step(f"{provider.display_name} is not logged in: modelmux login {provider.name}")
    console.step("Configure your tools: modelmux config litellm (or openai-python, langchain)")
    return 0
