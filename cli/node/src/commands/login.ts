/**
 * `modelmux login <provider>`: guided login, then a real test.
 *
 * The provider's own login command runs in a short-lived, hardened helper
 * container with only that provider's login volume. On Linux and macOS
 * Docker gives it a terminal (`-it`) fed straight from the user's terminal,
 * while its output passes through here so the login link can be spotted and
 * opened in the browser. On Windows (or with `--raw`) the user's console is
 * attached directly.
 *
 * Keystrokes, pasted codes and API keys are never stored or logged.
 */

import { randomBytes } from "node:crypto";

import { openBrowser } from "../browser.ts";
import type { Args } from "../cli.ts";
import type { Console } from "../console.ts";
import { type Context, makeContext } from "../context.ts";
import { CliError, DockerError, Interrupted, Timeout, UsageError } from "../errors.ts";
import * as health from "../health.ts";
import { interrupt, resetInterrupt } from "../interrupt.ts";
import { ask as askLine, readSecret } from "../prompt.ts";
import { getProvider, type LoginMethod, type Provider } from "../providers.ts";
import { registerSecret } from "../redact.ts";
import { resolveImage, serviceName } from "../stack.ts";
import { LinkScanner } from "../terminal.ts";
import { repr } from "../text.ts";
import * as upModule from "./up.ts";

export const SECRET_LOGIN_TIMEOUT = 120.0;

/** Replaceable in tests. */
export const io = {
  openBrowser,
  readSecret,
  ask: askLine,
  stdinIsTTY: (): boolean => process.stdin.isTTY === true,
};

export function useTty(args: Args): boolean {
  return process.platform !== "win32" && !args.raw;
}

export function chooseMethod(provider: Provider, requested: string | null): LoginMethod {
  const name = requested || provider.defaultLoginMethod;
  const method = provider.loginMethods.get(name);
  if (method === undefined) {
    const options = [...provider.loginMethods.values()].map((m) => `${m.name} (${m.description})`).join(", ");
    throw new UsageError(`${provider.displayName} has no login method ${repr(name)}.`, {
      hint: `Choose: ${options}`,
    });
  }
  return method;
}

/**
 * (Re)start the provider and wait for /health/ready: the server's startup
 * check makes one tiny real request, so ready means the login works.
 */
export async function ensureRunningAndTested(ctx: Context, provider: Provider): Promise<void> {
  const port = ctx.config.port(provider.name);
  const state = (await ctx.stack.states()).get(serviceName(provider));
  if (state !== undefined && state.state === "running" && (await health.server.ready(port))) {
    ctx.console.success(`${provider.displayName} is running at ${health.baseUrl(port)}/v1`);
  } else {
    await upModule.checkPorts(ctx, [provider]);
    await ctx.stack.compose(["up", "-d", "--force-recreate", serviceName(provider)], {
      timeout: upModule.COMPOSE_UP_TIMEOUT,
    });
    await upModule.startAndWait(ctx, provider);
  }
  ctx.console.success(`${provider.displayName} is logged in and tested.`);
  ctx.console.step("Configure your tools: modelmux config litellm (or openai-python, langchain)");
}

export function linkHandler(
  provider: Provider,
  noBrowser: boolean,
): (data: Buffer) => Promise<Buffer | null> {
  const scanner = new LinkScanner(provider.linkPattern);
  return async (data) => {
    const link = scanner.feed(data);
    if (link === null) return null;
    if (noBrowser) return Buffer.from("\r\n-> Open the link above in your browser.\r\n");
    let opened = false;
    try {
      opened = await io.openBrowser(link);
    } catch {
      opened = false; // no usable browser here
    }
    if (opened) return Buffer.from("\r\n-> Opened your browser. If it did not open, use the link above.\r\n");
    return Buffer.from("\r\n-> Could not open a browser here: open the link above yourself.\r\n");
  };
}

/**
 * Treat SIGTERM and SIGHUP (terminal closed) like Ctrl+C while logging in,
 * so the helper container is always removed and the user sees "Cancelled.".
 */
export async function cancelOnSignals<T>(work: () => Promise<T>): Promise<T> {
  if (process.platform === "win32") return work();
  const signals: NodeJS.Signals[] = ["SIGTERM", "SIGHUP"];
  const handler = (): void => interrupt();
  for (const sig of signals) process.on(sig, handler);
  try {
    return await work();
  } finally {
    for (const sig of signals) process.removeListener(sig, handler);
  }
}

async function interactiveLogin(
  ctx: Context,
  provider: Provider,
  method: LoginMethod,
  image: string,
  args: Args,
): Promise<number> {
  if (!io.stdinIsTTY()) {
    throw new UsageError("Logging in needs an interactive terminal.", {
      hint: `Run 'modelmux login ${provider.name}' in a terminal.`,
    });
  }
  const name = `modelmux-login-${provider.name}-${randomBytes(4).toString("hex")}`;
  const runArgs = ctx.stack.helperArgs(provider, image, method.command, ["-it", "--name", name]);
  try {
    return await cancelOnSignals(async () => {
      if (useTty(args)) {
        return ctx.docker.runTty(runArgs, {
          onOutput: linkHandler(provider, args.no_browser),
          timeout: provider.loginTimeout,
        });
      }
      ctx.console.step("Follow the instructions below; open the link they show in your browser.");
      return ctx.docker.passthrough(runArgs, { timeout: provider.loginTimeout });
    });
  } catch (error) {
    if (error instanceof Timeout || error instanceof DockerError) {
      await remove(ctx, name);
      if (error instanceof DockerError && !error.message.includes("in time")) throw error;
      throw new CliError("The login timed out.", { hint: `Start again: modelmux login ${provider.name}` });
    }
    if (error instanceof Interrupted) await remove(ctx, name);
    throw error;
  }
}

async function secretLogin(ctx: Context, provider: Provider, method: LoginMethod, image: string): Promise<number> {
  const prompt = method.secretStdin || "Secret";
  const secret = (await io.readSecret(`${prompt} (input is hidden): `)).trim();
  if (!secret) throw new UsageError(`No ${prompt} entered.`);
  registerSecret(secret);
  const runArgs = ctx.stack.helperArgs(provider, image, method.command, ["-i"]);
  const result = await ctx.docker.run(runArgs, {
    inputText: secret + "\n",
    check: false,
    timeout: SECRET_LOGIN_TIMEOUT,
  });
  return result.returncode;
}

/** Remove the helper container, even after Ctrl+C. */
async function remove(ctx: Context, container: string): Promise<void> {
  resetInterrupt(); // the cleanup itself must not be cancelled
  await ctx.docker.run(["rm", "-f", container], { check: false });
}

export async function run(args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const provider = getProvider(args.provider!);
  const method = chooseMethod(provider, args.method);
  const image = resolveImage(ctx.config);
  await upModule.prepare(ctx, image);
  const stack = ctx.stack;

  if (!args.force && (await stack.loggedIn(provider, image))) {
    console.success(`${provider.displayName} is already logged in (use --force to log in again).`);
    await ensureRunningAndTested(ctx, provider);
    return 0;
  }

  console.step(`Logging in to ${provider.displayName}: ${method.description}.`);
  if (method.secretStdin) await secretLogin(ctx, provider, method, image);
  else await interactiveLogin(ctx, provider, method, image, args);
  console.print();
  if (!(await stack.loggedIn(provider, image))) {
    throw new CliError(`The ${provider.displayName} login did not complete.`, {
      hint:
        `Try again: modelmux login ${provider.name} (add --raw to see only the ` +
        "provider's own output).",
    });
  }
  console.success(`Logged in to ${provider.displayName}.`);
  await ensureRunningAndTested(ctx, provider);
  return 0;
}
