/**
 * The CLI's settings (`config.json`): ports, and an optional image override.
 *
 * Everything has a default, so the file only exists once something is changed
 * or `modelmux up` runs. A damaged file is reported, never silently replaced.
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { CliError } from "./errors.ts";
import { writePrivate } from "./files.ts";
import { ensurePrivateDir } from "./paths.ts";
import { loadProviders } from "./providers.ts";
import { repr, sortedStrings } from "./text.ts";

export const CONFIG_FILE = "config.json";
export const CONFIG_VERSION = 1;

export class Config {
  ports: Map<string, number>;
  /** Explicit override; null means "the image pinned for this CLI". */
  image: string | null;

  constructor(ports: Map<string, number> = new Map(), image: string | null = null) {
    this.ports = ports;
    this.image = image;
  }

  port(provider: string): number {
    const port = this.ports.get(provider);
    if (port !== undefined) return port;
    const known = loadProviders().get(provider);
    if (known === undefined) throw new CliError(`Unknown provider ${repr(provider)}.`);
    return known.defaultPort;
  }
}

export class ConfigFileError extends CliError {
  constructor(file: string, problem: string) {
    super(`Your ModelMux settings file is damaged (${problem}).`, {
      hint: `Fix or delete ${file} and run the command again.`,
    });
  }
}

function parse(file: string, data: unknown): Config {
  if (
    typeof data !== "object" ||
    data === null ||
    Array.isArray(data) ||
    (data as Record<string, unknown>).version !== CONFIG_VERSION
  ) {
    throw new ConfigFileError(file, "unknown format version");
  }
  const record = data as Record<string, unknown>;
  const ports = record.ports ?? {};
  if (typeof ports !== "object" || ports === null || Array.isArray(ports)) {
    throw new ConfigFileError(file, "'ports' must be an object");
  }
  const known = loadProviders();
  const parsed = new Map<string, number>();
  for (const [name, port] of Object.entries(ports)) {
    if (!known.has(name)) throw new ConfigFileError(file, `unknown provider ${repr(name)} in 'ports'`);
    if (!Number.isInteger(port) || (port as number) < 1 || (port as number) > 65535) {
      throw new ConfigFileError(file, `invalid port for ${repr(name)}`);
    }
    parsed.set(name, port as number);
  }
  const image = record.image ?? null;
  if (image !== null && typeof image !== "string") {
    throw new ConfigFileError(file, "'image' must be a string");
  }
  return new Config(parsed, image);
}

export function loadConfig(directory: string): Config {
  const file = path.join(directory, CONFIG_FILE);
  if (!existsSync(file)) return new Config();
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(file, "utf8"));
  } catch {
    throw new ConfigFileError(file, "not valid JSON");
  }
  return parse(file, data);
}

/** Python's `json.dumps(data, indent=2)`: same spacing, non-ASCII escaped. */
export function pyJson(data: unknown): string {
  return JSON.stringify(data, null, 2).replace(
    /[\u0080-￿]/g,
    (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

export function saveConfig(directory: string, config: Config): string {
  ensurePrivateDir(directory);
  const file = path.join(directory, CONFIG_FILE);
  const ports: Record<string, number> = {};
  for (const name of sortedStrings(config.ports.keys())) ports[name] = config.ports.get(name)!;
  const data: Record<string, unknown> = { version: CONFIG_VERSION, ports };
  if (config.image !== null) data.image = config.image;
  writePrivate(file, pyJson(data) + "\n");
  return file;
}
