/** `modelmux down`, `logs` and `status`. */

import { existsSync } from "node:fs";

import type { Args } from "../cli.ts";
import type { Console } from "../console.ts";
import { makeContext } from "../context.ts";
import { CliError } from "../errors.ts";
import * as health from "../health.ts";
import { resolveImage, serviceName } from "../stack.ts";

export const NOT_SET_UP = "ModelMux is not set up on this machine yet.";

export async function down(_args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  if (!existsSync(ctx.stack.composeFile)) {
    console.success("ModelMux is not running.");
    return 0;
  }
  await ctx.stack.compose(["down"], { timeout: 120 });
  console.success("Stopped ModelMux. Your logins are kept.");
  return 0;
}

export async function logs(args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const stack = ctx.stack;
  if (!existsSync(stack.composeFile)) throw new CliError(NOT_SET_UP, { hint: "Start it with: modelmux up" });
  const services = args.provider ? [serviceName(ctx.providers.get(args.provider)!)] : [];
  const follow = args.follow ? ["--follow"] : [];
  return ctx.docker.passthrough([
    "compose", "-p", "modelmux", "-f", stack.composeFile,
    "logs", args.provider ? "--no-log-prefix" : "--timestamps",
    "--tail", String(Math.max(args.tail, 0)), ...follow, ...services,
  ]);
}

export async function status(_args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  await ctx.docker.checkAvailable();
  const stack = ctx.stack;
  const states = await stack.states();
  let image: string | null;
  try {
    image = resolveImage(ctx.config);
  } catch (error) {
    if (!(error instanceof CliError)) throw error;
    image = null;
  }

  const rows: string[][] = [["PROVIDER", "CONTAINER", "LOGIN", "HEALTH", "URL"]];
  for (const [name, provider] of ctx.providers) {
    const state = states.get(serviceName(provider));
    const port = ctx.config.port(name);
    const container = state ? state.state : "not started";
    const login = image === null ? "?" : (await stack.loggedIn(provider, image)) ? "yes" : "no";
    let healthy: string;
    if (state && state.state === "running") healthy = (await health.server.ready(port)) ? "ready" : "starting";
    else healthy = "-";
    const url = healthy === "ready" ? `${health.baseUrl(port)}/v1` : "-";
    rows.push([name, container, login, healthy, url]);
  }

  const widths = rows[0]!.map((_, i) => Math.max(...rows.map((row) => row[i]!.length)));
  for (const row of rows) {
    console.print(row.map((cell, i) => cell.padEnd(widths[i]!)).join("  ").trimEnd());
  }

  if (image === null) console.step("No server image chosen yet: run 'modelmux up' first.");
  for (const [name, provider] of ctx.providers) {
    const state = states.get(serviceName(provider));
    if (state && ["exited", "dead"].includes(state.state)) {
      console.step(`${provider.displayName} has stopped. If its login expired: modelmux login ${name}`);
    }
  }
  return 0;
}
