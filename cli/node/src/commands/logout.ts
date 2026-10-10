/** `modelmux logout <provider>`: stop it and delete its saved login. */

import { existsSync } from "node:fs";

import type { Args } from "../cli.ts";
import type { Console } from "../console.ts";
import { makeContext } from "../context.ts";
import { CliError, UsageError } from "../errors.ts";
import { ask } from "../prompt.ts";
import { getProvider } from "../providers.ts";
import { resolveImage, serviceName, volumeName } from "../stack.ts";

/** Replaceable in tests. */
export const io = {
  ask,
  stdinIsTTY: (): boolean => process.stdin.isTTY === true,
};

export async function confirm(question: string): Promise<boolean> {
  if (!io.stdinIsTTY()) {
    throw new UsageError("Logout needs confirmation.", {
      hint: "Add --yes to confirm when not in a terminal.",
    });
  }
  return ["y", "yes"].includes((await io.ask(`${question} [y/N] `)).trim().toLowerCase());
}

export async function run(args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const provider = getProvider(args.provider!);
  const stack = ctx.stack;
  if (!(await stack.volumeExists(provider))) {
    console.success(`${provider.displayName} is not logged in; nothing to remove.`);
    return 0;
  }
  if (!args.yes && !(await confirm(`Remove the saved ${provider.displayName} login?`))) {
    console.step("Nothing changed.");
    return 0;
  }

  if (existsSync(stack.composeFile)) {
    await stack.compose(["rm", "--stop", "--force", serviceName(provider)], { check: false });
  }
  let image: string | null;
  try {
    image = resolveImage(ctx.config);
  } catch (error) {
    if (!(error instanceof CliError)) throw error;
    image = null;
  }
  if (image !== null) {
    // Best effort: lets the provider revoke the token, not just forget it.
    const result = await ctx.docker.run(stack.helperArgs(provider, image, provider.logoutCommand), {
      check: false,
      timeout: 60,
    });
    if (!result.ok) console.detail("The provider's own logout did not succeed; removing the login anyway.");
  }
  await ctx.docker.run(["volume", "rm", volumeName(provider)]);
  console.success(`Logged out of ${provider.displayName} and removed its saved login.`);
  return 0;
}
