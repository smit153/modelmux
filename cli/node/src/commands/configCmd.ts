/** `modelmux key show` and `modelmux config <target>`. */

import type { Args } from "../cli.ts";
import { KEY_ENV, loadTemplate, render, type Target } from "../clientconfig.ts";
import type { Console } from "../console.ts";
import { makeContext } from "../context.ts";
import { CliError } from "../errors.ts";
import * as health from "../health.ts";
import { getProvider } from "../providers.ts";
import { type ApiKey, readApiKey } from "../secretsStore.ts";

function key(home: string): ApiKey {
  const found = readApiKey(home);
  if (found === null) {
    throw new CliError("ModelMux is not set up yet, so there is no API key.", { hint: "Run: modelmux up" });
  }
  return found;
}

export async function keyShow(_args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  console.reveal(key(ctx.home).value);
  console.note("Treat this like a password. Clients send it as: Authorization: Bearer <key>");
  return 0;
}

export async function modelsFor(port: number, apiKey: string): Promise<string[]> {
  const [status, body] = await health.server.getJson(port, "/v1/models", { apiKey });
  if (status !== 200 || typeof body !== "object" || body === null || Array.isArray(body)) return [];
  const data = (body as Record<string, unknown>).data;
  if (!Array.isArray(data)) return [];
  return data
    .filter((m): m is { id: string } =>
      typeof m === "object" && m !== null && !Array.isArray(m) && typeof (m as Record<string, unknown>).id === "string")
    .map((m) => m.id);
}

export async function config(args: Args, console: Console): Promise<number> {
  const ctx = makeContext(console);
  const template = loadTemplate(args.target);
  const apiKey = key(ctx.home);
  const names = args.provider ? [args.provider] : [...ctx.providers.keys()];
  const targets: Target[] = [];
  for (const name of names) {
    const provider = getProvider(name);
    const port = ctx.config.port(name);
    let models = await modelsFor(port, apiKey.value);
    if (!models.length) {
      models = [provider.exampleModel];
      console.note(
        `${provider.displayName} is not running; using its example model '${provider.exampleModel}'.`,
      );
    }
    targets.push({ provider: name, displayName: provider.displayName, baseUrl: `${health.baseUrl(port)}/v1`, models });
  }
  const text = render(template, targets, args.reveal_key ? apiKey.value : null);
  if (args.reveal_key) {
    console.reveal(text.replace(/\n+$/, ""));
  } else {
    console.print(text.replace(/\n+$/, ""));
    if (template.keyStyle !== "env") {
      console.note(`Set ${KEY_ENV} first: export ${KEY_ENV}=$(modelmux key show)`);
    } else {
      console.note("Add the key yourself (modelmux key show), or use --reveal-key.");
    }
  }
  return 0;
}
