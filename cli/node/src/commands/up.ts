/** `modelmux up`: prepare everything and start the logged-in providers. */

import type { Args } from "../cli.ts";
import { saveConfig } from "../config.ts";
import type { Console } from "../console.ts";
import { type Context, makeContext } from "../context.ts";
import { CliError, UsageError } from "../errors.ts";
import * as health from "../health.ts";
import { ensurePrivateDir } from "../paths.ts";
import { portFree } from "../ports.ts";
import type { Provider } from "../providers.ts";
import { ensureApiKey } from "../secretsStore.ts";
import { buildCompose, resolveImage, serviceName, writeCompose } from "../stack.ts";
import { repr } from "../text.ts";

export const READY_TIMEOUT = 240.0;
export const POLL_INTERVAL = 0.5;
export const COMPOSE_UP_TIMEOUT = 300.0;

export function parsePortOverrides(values: readonly string[], known: ReadonlySet<string>): Map<string, number> {
  const ports = new Map<string, number>();
  for (const value of values) {
    const at = value.indexOf("=");
    const name = at < 0 ? value : value.slice(0, at);
    const raw = at < 0 ? "" : value.slice(at + 1);
    if (at < 0 || !known.has(name) || !/^\d+$/.test(raw) || Number(raw) < 1024 || Number(raw) > 65535) {
      throw new UsageError(`Invalid --port ${repr(value)}.`, {
        hint: "Use --port <provider>=<1024-65535>, e.g. claude=9101.",
      });
    }
    ports.set(name, Number(raw));
  }
  return ports;
}

/** Validate and remember --image / --port; return the image to run. */
export function applySettings(ctx: Context, args: Args): string {
  const config = ctx.config;
  const image = resolveImage(config, args.image);
  const ports = parsePortOverrides(args.port ?? [], new Set(ctx.providers.keys()));
  const changed = ports.size > 0 || (args.image !== null && args.image !== config.image);
  for (const [name, port] of ports) config.ports.set(name, port);
  if (args.image !== null) config.image = args.image;
  const used = [...ctx.providers.keys()].map((name) => config.port(name));
  if (new Set(used).size !== used.length) {
    throw new UsageError("Two providers are set to the same port.", {
      hint: "Choose different --port values.",
    });
  }
  if (changed) saveConfig(ctx.home, config);
  return image;
}

export async function startAndWait(ctx: Context, provider: Provider): Promise<void> {
  const { console, stack } = ctx;
  const service = serviceName(provider);
  const port = ctx.config.port(provider.name);

  const stopped = async (): Promise<boolean> => {
    const state = (await stack.states()).get(service);
    return state !== undefined && ["exited", "dead"].includes(state.state);
  };

  console.step(`Starting ${provider.displayName} (it checks its login, about 10-30 s)...`);
  const ok = await health.waitUntil(() => health.ready(port), {
    timeout: READY_TIMEOUT,
    interval: POLL_INTERVAL,
    shouldStop: stopped,
  });
  if (!ok) {
    if (await stopped()) {
      throw new CliError(`${provider.displayName} stopped while starting.`, {
        hint:
          `Its login may have expired: modelmux login ${provider.name}. ` +
          `Details: modelmux logs ${provider.name}`,
      });
    }
    throw new CliError(`${provider.displayName} did not become ready in time.`, {
      hint: `See what it is doing: modelmux logs ${provider.name}`,
    });
  }
  console.success(`${provider.displayName} is running at ${health.baseUrl(port)}/v1`);
}

/**
 * Docker, API key, compose file, image and login volumes: all idempotent.
 *
 * Returns true if the compose file changed (so running containers must be
 * recreated to pick it up).
 */
export async function prepare(ctx: Context, image: string): Promise<boolean> {
  const { console, stack } = ctx;
  console.step("Checking Docker...");
  await ctx.docker.checkAvailable();
  ensurePrivateDir(ctx.home);
  const key = ensureApiKey(ctx.home);
  if (key.created) {
    console.success("Created an API key for your clients (see it with: modelmux key show).");
  }
  const changed = writeCompose(ctx.home, buildCompose(ctx.providers, ctx.config, image, key.path));
  if (!(await stack.imagePresent(image))) {
    console.step(`Downloading the ModelMux server image ${image} (first time only, ~1.4 GB)...`);
    await stack.ensureImage(image);
  }
  for (const provider of ctx.providers.values()) await stack.ensureVolume(provider);
  return changed || key.created;
}

/**
 * Providers whose container runs and answers /health/ready. Their login is
 * proven (the server's startup check made a real request), so no helper
 * container is needed to check it again.
 */
export async function runningAndReady(ctx: Context, providers: readonly Provider[]): Promise<Set<string>> {
  const states = await ctx.stack.states();
  const ready = new Set<string>();
  for (const p of providers) {
    const s = states.get(serviceName(p));
    if (s !== undefined && s.state === "running" && (await health.ready(ctx.config.port(p.name)))) {
      ready.add(p.name);
    }
  }
  return ready;
}

/**
 * Logged in or not, per provider: running ones count as logged in, the rest
 * are checked in parallel helper containers.
 */
export async function checkLogins(
  ctx: Context,
  providers: readonly Provider[],
  image: string,
): Promise<Map<string, boolean>> {
  const ready = await runningAndReady(ctx, providers);
  const unknown = providers.filter((p) => !ready.has(p.name));
  const answers = await settleAll(unknown.map((p) => ctx.stack.loggedIn(p, image)));
  const result = new Map<string, boolean>();
  for (const p of providers) if (ready.has(p.name)) result.set(p.name, true);
  unknown.forEach((p, i) => result.set(p.name, answers[i]!));
  return result;
}

/** Wait for all promises, then return their values or throw the first failure (in order). */
async function settleAll<T>(promises: readonly Promise<T>[]): Promise<T[]> {
  const settled = await Promise.allSettled(promises);
  for (const outcome of settled) if (outcome.status === "rejected") throw outcome.reason;
  return settled.map((outcome) => (outcome as PromiseFulfilledResult<T>).value);
}

/** Wait for every provider at once; report the first failure afterwards. */
export async function waitAll(ctx: Context, providers: readonly Provider[]): Promise<void> {
  await settleAll(providers.map((p) => startAndWait(ctx, p)));
}

export async function checkPorts(ctx: Context, providers: readonly Provider[]): Promise<void> {
  const states = await ctx.stack.states();
  for (const provider of providers) {
    const running = states.get(serviceName(provider));
    const port = ctx.config.port(provider.name);
    if ((running === undefined || running.state !== "running") && !(await portFree(port))) {
      throw new CliError(
        `Port ${port} for ${provider.displayName} is already in use by another program.`,
        {
          hint:
            `Pick another port: modelmux up --port ${provider.name}=<port>, ` +
            "or run 'modelmux doctor'.",
        },
      );
    }
  }
}

export async function run(args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const providers = ctx.providers;
  const unknown = args.providers.filter((name) => !providers.has(name));
  if (unknown.length) {
    throw new UsageError(`Unknown provider ${repr(unknown[0]!)}.`, {
      hint: `Choose from: ${[...providers.keys()].join(", ")}.`,
    });
  }
  const image = applySettings(ctx, args);
  const composeChanged = await prepare(ctx, image);
  const names = args.providers.length ? args.providers : [...providers.keys()];
  const targets = names.map((name) => providers.get(name)!);
  console.step("Checking logins...");
  const loggedIn = await checkLogins(ctx, targets, image);
  const missing = targets.filter((p) => !loggedIn.get(p.name));
  if (args.providers.length && missing.length) {
    throw new CliError(`${missing[0]!.displayName} is not logged in yet.`, {
      hint: `Log in first: modelmux login ${missing[0]!.name}`,
    });
  }
  const toStart = targets.filter((p) => loggedIn.get(p.name));
  if (!toStart.length) {
    console.success("ModelMux is set up. No provider is logged in yet.");
    console.step(`Next: modelmux login ${targets[0]!.name}`);
    return 0;
  }
  const ready = composeChanged ? new Set<string>() : await runningAndReady(ctx, toStart);
  const waiting = toStart.filter((p) => !ready.has(p.name));
  if (waiting.length) {
    await checkPorts(ctx, waiting);
    await ctx.stack.compose(["up", "-d", ...toStart.map(serviceName)], { timeout: COMPOSE_UP_TIMEOUT });
  }
  for (const provider of toStart) {
    if (ready.has(provider.name)) {
      const port = ctx.config.port(provider.name);
      console.success(`${provider.displayName} is already running at ${health.baseUrl(port)}/v1`);
    }
  }
  await waitAll(ctx, waiting);
  for (const provider of missing) {
    console.step(`${provider.displayName} is not logged in: modelmux login ${provider.name}`);
  }
  console.step("Configure your tools: modelmux config litellm (or openai-python, langchain)");
  return 0;
}
