/** `modelmux doctor`: diagnose common problems, each with a suggested fix. */

import { existsSync, statSync } from "node:fs";
import path from "node:path";

import type { Args } from "../cli.ts";
import type { Console } from "../console.ts";
import { type Context, makeContext } from "../context.ts";
import { CliError, DockerError } from "../errors.ts";
import * as health from "../health.ts";
import { ports } from "../ports.ts";
import type { Provider } from "../providers.ts";
import { readApiKey, SECRETS_FILE } from "../secretsStore.ts";
import { resolveImage, serviceName } from "../stack.ts";
import * as updates from "../updates.ts";
import { VERSION } from "../version.ts";

export class Report {
  readonly console: Console;
  failures = 0;
  warnings = 0;

  constructor(console: Console) {
    this.console = console;
  }

  ok(text: string): void {
    this.console.success(text);
  }

  warn(text: string, hint: string | null = null): void {
    this.warnings += 1;
    this.console.warn(text + (hint ? `\n    ${hint}` : ""));
  }

  fail(text: string, hint: string | null = null): void {
    this.failures += 1;
    this.console.error(text, hint);
  }
}

/** The server's own reason for refusing to start, from its recent logs. */
export async function startupFailureReason(ctx: Context, provider: Provider): Promise<string | null> {
  const result = await ctx.stack.compose(
    ["logs", "--no-log-prefix", "--tail", "200", serviceName(provider)],
    { check: false },
  );
  let reason: string | null = null;
  for (const line of result.stdout.split(/\r\n|\r|\n/)) {
    let entry: unknown;
    try {
      entry = JSON.parse(line);
    } catch {
      continue;
    }
    if (typeof entry === "object" && entry !== null && !Array.isArray(entry)) {
      const record = entry as Record<string, unknown>;
      if (record.event === "probe_failed") {
        reason = typeof record.reason === "string" ? record.reason : reason;
      }
    }
  }
  return reason;
}

async function checkDocker(ctx: Context, report: Report): Promise<boolean> {
  let version: string;
  try {
    version = await ctx.docker.checkAvailable();
  } catch (error) {
    if (!(error instanceof DockerError)) throw error;
    report.fail(error.message, error.hint);
    return false;
  }
  report.ok(`Docker ${version} is running, with Compose v2`);
  return true;
}

async function checkNetworkAndVersion(report: Report): Promise<void> {
  if (await updates.registry.registryReachable()) {
    report.ok("The image registry (ghcr.io) is reachable");
  } else {
    report.warn(
      "Cannot reach ghcr.io (needed to download the server image)",
      "Check your connection or proxy (HTTPS_PROXY, and Docker's proxy settings).",
    );
  }
  const latest = await updates.registry.latestVersion();
  if (latest === null) {
    report.warn("Could not check npm for a newer modelmux-cli");
  } else if (updates.newerAvailable(VERSION, latest)) {
    report.warn(`modelmux-cli ${latest} is available (you have ${VERSION})`, updates.UPGRADE_HINT);
  } else {
    report.ok(`modelmux-cli ${VERSION} is the latest version`);
  }
}

function checkFiles(ctx: Context, report: Report): boolean {
  const keyPath = path.join(ctx.home, SECRETS_FILE);
  if (!existsSync(keyPath)) {
    report.fail("ModelMux is not set up on this machine yet", "Run: modelmux up");
    return false;
  }
  if (process.platform !== "win32") {
    for (const [file, mode] of [[ctx.home, 0o700], [keyPath, 0o600]] as const) {
      if ((statSync(file).mode & 0o7777) !== mode) {
        report.fail(`${file} is readable by other users`, `Fix it: chmod ${mode.toString(8)} '${file}'`);
        return true;
      }
    }
  }
  report.ok(`Settings and API key are in ${ctx.home}`);
  return true;
}

async function portConflict(ctx: Context, provider: Provider, report: Report): Promise<boolean> {
  const port = ctx.config.port(provider.name);
  if (await ports.portFree(port)) return false;
  report.fail(
    `${provider.displayName}: port ${port} is used by another program`,
    `Choose another: modelmux up --port ${provider.name}=<port>`,
  );
  return true;
}

export function stoppedHint(provider: Provider, reason: string | null): string {
  if (reason && reason.includes("auth")) {
    return `The login has expired or was revoked: modelmux login ${provider.name} --force`;
  }
  if (reason && ["version", "flag", "feature", "config key"].some((word) => reason.includes(word))) {
    return "The CLI inside the image is not supported: modelmux upgrade";
  }
  return `See its logs: modelmux logs ${provider.name}`;
}

async function checkRunning(ctx: Context, provider: Provider, apiKey: string, report: Report): Promise<void> {
  const { name, displayName: display } = provider;
  const port = ctx.config.port(name);
  if (!(await health.server.ready(port))) {
    report.warn(`${display}: running but not ready yet`, `Watch it: modelmux logs ${name} -f`);
    return;
  }
  const [status] = await health.server.getJson(port, "/v1/models", { apiKey });
  if (status === 200) {
    report.ok(`${display}: logged in, running and answering at ${health.baseUrl(port)}/v1`);
  } else if (status === 401) {
    report.fail(
      `${display}: the server does not accept the saved API key`,
      "Recreate it with the current key: modelmux up",
    );
  } else {
    report.fail(`${display}: the API did not answer correctly (HTTP ${status})`, `See its logs: modelmux logs ${name}`);
  }
}

async function checkProvider(
  ctx: Context,
  provider: Provider,
  image: string,
  apiKey: string,
  report: Report,
): Promise<void> {
  const display = provider.displayName;
  if (!(await ctx.stack.loggedIn(provider, image))) {
    report.warn(`${display}: not logged in`, `Run: modelmux login ${provider.name}`);
    await portConflict(ctx, provider, report);
    return;
  }
  const state = (await ctx.stack.states()).get(serviceName(provider));
  if (state === undefined) {
    if (!(await portConflict(ctx, provider, report))) {
      report.warn(`${display}: logged in but not running`, "Start it: modelmux up");
    }
    return;
  }
  if (state.state !== "running") {
    const reason = await startupFailureReason(ctx, provider);
    const detail = reason ? ` (${reason})` : "";
    report.fail(`${display}: the server stopped${detail}`, stoppedHint(provider, reason));
    return;
  }
  await checkRunning(ctx, provider, apiKey, report);
}

export async function run(_args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const report = new Report(console);
  const dockerOk = await checkDocker(ctx, report);
  await checkNetworkAndVersion(report);
  const setUp = checkFiles(ctx, report);
  if (dockerOk && setUp) {
    let image: string | null = null;
    try {
      image = resolveImage(ctx.config);
    } catch (error) {
      if (!(error instanceof CliError)) throw error;
      report.fail(error.message, error.hint);
    }
    if (image !== null) {
      if (await ctx.stack.imagePresent(image)) report.ok(`Server image ${image} is present`);
      else report.warn(`Server image ${image} is not downloaded yet`, "Run: modelmux up");
      const key = readApiKey(ctx.home);
      for (const provider of ctx.providers.values()) {
        await checkProvider(ctx, provider, image, key ? key.value : "", report);
      }
    }
  }

  console.print();
  if (report.failures) {
    console.error(`${report.failures} problem(s) found.`);
    return 1;
  }
  if (report.warnings) console.success(`No problems, ${report.warnings} note(s).`);
  else console.success("Everything looks good.");
  return 0;
}
