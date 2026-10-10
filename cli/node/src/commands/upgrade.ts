/**
 * `modelmux upgrade`: run the server image that matches this CLI, keeping logins.
 *
 * The image version follows the CLI version: a newer server comes with a newer
 * modelmux-cli. This command makes sure the image pinned for the installed CLI
 * (or an explicit `--image`) is present, recreates the providers that were
 * running, and says when a newer CLI is available.
 */

import { existsSync } from "node:fs";

import type { Args } from "../cli.ts";
import { saveConfig } from "../config.ts";
import type { Console } from "../console.ts";
import { makeContext } from "../context.ts";
import { resolveImage, serviceName, type ServiceState } from "../stack.ts";
import * as updates from "../updates.ts";
import { VERSION } from "../version.ts";
import * as upModule from "./up.ts";

export async function run(args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const image = resolveImage(ctx.config, args.image);
  if (args.image !== null && args.image !== ctx.config.image) {
    ctx.config.image = args.image;
    saveConfig(ctx.home, ctx.config);
  }

  const states: Map<string, ServiceState> = existsSync(ctx.stack.composeFile)
    ? await ctx.stack.states()
    : new Map();
  await upModule.prepare(ctx, image); // downloads the image if needed, rewrites compose.yaml
  console.success(`Server image: ${image}`);

  const running = [...ctx.providers.values()].filter(
    (p) => states.get(serviceName(p))?.state === "running",
  );
  if (running.length) {
    await ctx.stack.compose(["up", "-d", ...running.map(serviceName)], {
      timeout: upModule.COMPOSE_UP_TIMEOUT,
    });
    for (const provider of running) await upModule.startAndWait(ctx, provider);
    console.success("Upgraded. Logins were kept.");
  } else {
    console.success("Nothing was running; the next 'modelmux up' uses this image.");
  }

  const latest = await updates.registry.latestVersion();
  if (updates.newerAvailable(VERSION, latest)) {
    console.step(
      `modelmux-cli ${latest} is available (you have ${VERSION}). ` +
        `It comes with a newer server: ${updates.UPGRADE_HINT}`,
    );
  }
  return 0;
}
