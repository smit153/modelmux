"""``modelmux doctor``: diagnose common problems, each with a suggested fix."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass, field

from modelmux_cli import __version__, health, updates
from modelmux_cli.console import Console
from modelmux_cli.context import Context, make_context
from modelmux_cli.errors import CliError, DockerError
from modelmux_cli.ports import port_free
from modelmux_cli.providers import Provider
from modelmux_cli.secrets_store import SECRETS_FILE, read_api_key
from modelmux_cli.stack import resolve_image, service_name


@dataclass
class Report:
    console: Console
    failures: int = 0
    warnings: int = 0
    lines: list[str] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.console.success(text)

    def warn(self, text: str, hint: str | None = None) -> None:
        self.warnings += 1
        self.console.warn(text + (f"\n    {hint}" if hint else ""))

    def fail(self, text: str, hint: str | None = None) -> None:
        self.failures += 1
        self.console.error(text, hint)


def startup_failure_reason(ctx: Context, provider: Provider) -> str | None:
    """The server's own reason for refusing to start, from its recent logs."""
    result = ctx.stack.compose(
        "logs", "--no-log-prefix", "--tail", "200", service_name(provider), check=False
    )
    reason = None
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("event") == "probe_failed":
            value = entry.get("reason")
            reason = value if isinstance(value, str) else reason
    return reason


def check_docker(ctx: Context, report: Report) -> bool:
    try:
        version = ctx.docker.check_available()
    except DockerError as exc:
        report.fail(exc.message, exc.hint)
        return False
    report.ok(f"Docker {version} is running, with Compose v2")
    return True


def check_network_and_version(report: Report) -> None:
    if updates.registry_reachable():
        report.ok("The image registry (ghcr.io) is reachable")
    else:
        report.warn(
            "Cannot reach ghcr.io (needed to download the server image)",
            "Check your connection or proxy (HTTPS_PROXY, and Docker's proxy settings).",
        )
    latest = updates.latest_version()
    if latest is None:
        report.warn("Could not check PyPI for a newer modelmux-cli")
    elif updates.newer_available(__version__, latest):
        report.warn(f"modelmux-cli {latest} is available (you have {__version__})",
                    updates.UPGRADE_HINT)  # fmt: skip
    else:
        report.ok(f"modelmux-cli {__version__} is the latest version")


def check_files(ctx: Context, report: Report) -> bool:
    key_path = ctx.home / SECRETS_FILE
    if not key_path.exists():
        report.fail("ModelMux is not set up on this machine yet", "Run: modelmux up")
        return False
    if os.name == "posix":
        for path, mode in ((ctx.home, 0o700), (key_path, 0o600)):
            if stat.S_IMODE(path.stat().st_mode) != mode:
                report.fail(
                    f"{path} is readable by other users", f"Fix it: chmod {mode:o} '{path}'"
                )
                return True
    report.ok(f"Settings and API key are in {ctx.home}")
    return True


def port_conflict(ctx: Context, provider: Provider, report: Report) -> bool:
    port = ctx.config.port(provider.name)
    if port_free(port):
        return False
    report.fail(
        f"{provider.display_name}: port {port} is used by another program",
        f"Choose another: modelmux up --port {provider.name}=<port>",
    )
    return True


def stopped_hint(provider: Provider, reason: str | None) -> str:
    if reason and "auth" in reason:
        return f"The login has expired or was revoked: modelmux login {provider.name} --force"
    if reason and any(word in reason for word in ("version", "flag", "feature", "config key")):
        return "The CLI inside the image is not supported: modelmux upgrade"
    return f"See its logs: modelmux logs {provider.name}"


def check_running(ctx: Context, provider: Provider, api_key: str, report: Report) -> None:
    name, display = provider.name, provider.display_name
    port = ctx.config.port(name)
    if not health.ready(port):
        report.warn(f"{display}: running but not ready yet", f"Watch it: modelmux logs {name} -f")
        return
    status, _ = health.get_json(port, "/v1/models", api_key=api_key)
    if status == 200:
        report.ok(f"{display}: logged in, running and answering at {health.base_url(port)}/v1")
    elif status == 401:
        report.fail(
            f"{display}: the server does not accept the saved API key",
            "Recreate it with the current key: modelmux up",
        )
    else:
        report.fail(
            f"{display}: the API did not answer correctly (HTTP {status})",
            f"See its logs: modelmux logs {name}",
        )


def check_provider(
    ctx: Context, provider: Provider, image: str, api_key: str, report: Report
) -> None:
    display = provider.display_name
    if not ctx.stack.logged_in(provider, image):
        report.warn(f"{display}: not logged in", f"Run: modelmux login {provider.name}")
        port_conflict(ctx, provider, report)
        return
    state = ctx.stack.states().get(service_name(provider))
    if state is None:
        if not port_conflict(ctx, provider, report):
            report.warn(f"{display}: logged in but not running", "Start it: modelmux up")
        return
    if state.state != "running":
        reason = startup_failure_reason(ctx, provider)
        detail = f" ({reason})" if reason else ""
        report.fail(f"{display}: the server stopped{detail}", stopped_hint(provider, reason))
        return
    check_running(ctx, provider, api_key, report)


def run(args: argparse.Namespace, console: Console) -> int:
    ctx = make_context(console)
    report = Report(console)
    docker_ok = check_docker(ctx, report)
    check_network_and_version(report)
    set_up = check_files(ctx, report)
    if docker_ok and set_up:
        try:
            image = resolve_image(ctx.config)
        except CliError as exc:
            report.fail(exc.message, exc.hint)
        else:
            if ctx.stack.image_present(image):
                report.ok(f"Server image {image} is present")
            else:
                report.warn(f"Server image {image} is not downloaded yet", "Run: modelmux up")
            key = read_api_key(ctx.home)
            for provider in sorted(ctx.providers.values(), key=lambda p: p.name):
                check_provider(ctx, provider, image, key.value if key else "", report)

    console.print()
    if report.failures:
        console.error(f"{report.failures} problem(s) found.")
        return 1
    if report.warnings:
        console.success(f"No problems, {report.warnings} note(s).")
    else:
        console.success("Everything looks good.")
    return 0
