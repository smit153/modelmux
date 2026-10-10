/**
 * The API key clients use to call ModelMux (not a provider credential).
 *
 * Generated once with `crypto`, stored in `secrets.env` with mode 0600 in the
 * format compose reads as an env file. The value is registered for redaction
 * as soon as it is loaded, so it never appears in output unless the user
 * explicitly asks for it (`modelmux key show`).
 */

import { randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { CliError } from "./errors.ts";
import { tighten, writePrivate } from "./files.ts";
import { ensurePrivateDir } from "./paths.ts";
import { registerSecret } from "./redact.ts";

export const SECRETS_FILE = "secrets.env";
export const VARIABLE = "MODELMUX_API_KEYS";
export const KEY_BYTES = 32; // base64url(32 bytes) -> 43 characters; the server requires >= 32
const LINE = new RegExp(`^${VARIABLE}=([A-Za-z0-9_-]{32,})$`);

export interface ApiKey {
  readonly value: string;
  readonly path: string;
  readonly created: boolean;
  readonly permissionsFixed: boolean;
}

export class SecretsFileError extends CliError {
  constructor(file: string) {
    super("The saved ModelMux API key is unreadable or damaged.", {
      hint:
        `Delete ${file} and run 'modelmux up' to create a new key ` +
        "(clients using the old key will need the new one).",
    });
  }
}

export function readApiKey(directory: string): ApiKey | null {
  const file = path.join(directory, SECRETS_FILE);
  if (!existsSync(file)) return null;
  const fixed = tighten(file);
  let lines: string[];
  try {
    lines = readFileSync(file, "utf8")
      .split(/\r\n|\r|\n/)
      .map((ln) => ln.trim())
      .filter((ln) => ln !== "");
  } catch {
    throw new SecretsFileError(file);
  }
  const match = lines.length === 1 ? LINE.exec(lines[0]!) : null;
  if (match === null) throw new SecretsFileError(file);
  const value = match[1]!;
  registerSecret(value);
  return { value, path: file, created: false, permissionsFixed: fixed };
}

/** Return the saved key, creating one on first use. */
export function ensureApiKey(directory: string): ApiKey {
  const existing = readApiKey(directory);
  if (existing !== null) return existing;
  ensurePrivateDir(directory);
  const value = randomBytes(KEY_BYTES).toString("base64url");
  registerSecret(value);
  const file = path.join(directory, SECRETS_FILE);
  writePrivate(file, `${VARIABLE}=${value}\n`);
  return { value, path: file, created: true, permissionsFixed: false };
}
