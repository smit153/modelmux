/**
 * Where the CLI keeps its own files.
 *
 * | OS      | Directory                                          |
 * |---------|----------------------------------------------------|
 * | Linux   | `$XDG_CONFIG_HOME/modelmux` (`~/.config/modelmux`) |
 * | macOS   | `~/Library/Application Support/modelmux`           |
 * | Windows | `%APPDATA%\modelmux`                               |
 *
 * `MODELMUX_CLI_HOME` overrides it (tests, portable setups). The directory
 * holds only small files: `config.json`, `secrets.env` (mode 0600) and the
 * generated `compose.yaml`. Logins never live here: they stay in Docker
 * volumes. The same directory as the Python CLI, so both share one setup.
 */

import { chmodSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

export const APP_NAME = "modelmux";
export const OVERRIDE_ENV = "MODELMUX_CLI_HOME";

function expandUser(value: string, home: string): string {
  if (value === "~") return home;
  if (value.startsWith("~/") || value.startsWith("~\\")) return path.join(home, value.slice(2));
  return value;
}

export function configDir(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
  home: string = homedir(),
): string {
  const override = env[OVERRIDE_ENV];
  if (override) return expandUser(override, home);
  const p = platform === "win32" ? path.win32 : path.posix;
  let base: string;
  if (platform === "win32") {
    base = env.APPDATA ? env.APPDATA : p.join(home, "AppData", "Roaming");
  } else if (platform === "darwin") {
    base = p.join(home, "Library", "Application Support");
  } else {
    const xdg = env.XDG_CONFIG_HOME;
    base = xdg && path.posix.isAbsolute(xdg) ? xdg : p.join(home, ".config");
  }
  return p.join(base, APP_NAME);
}

/** Create `dir` if needed; on POSIX make it accessible to the owner only. */
export function ensurePrivateDir(dir: string): string {
  mkdirSync(dir, { recursive: true });
  if (process.platform !== "win32") chmodSync(dir, 0o700);
  return dir;
}
