/**
 * Open a link in the user's browser (Python's `webbrowser.open`).
 *
 * The second and last module that starts programs (besides docker.ts): the
 * system's own opener, as an argument list without a shell, detached so it
 * never holds the login up. Resolves false when there is no usable browser
 * here (no desktop session, opener missing), so the caller can say so.
 */

import { spawn } from "node:child_process";

import { which } from "./docker.ts";

/** The command to run for `url`, or null when no browser can be used here. */
export function browserCommand(
  url: string,
  env: NodeJS.ProcessEnv = process.env,
  platform: string = process.platform,
): string[] | null {
  // $BROWSER: a list of commands; "%s" marks where the link goes.
  for (const entry of (env.BROWSER ?? "").split(platform === "win32" ? ";" : ":")) {
    const parts = entry.trim().split(/\s+/).filter(Boolean);
    if (parts.length && which(parts[0]!, env, platform)) {
      return parts.some((p) => p.includes("%s"))
        ? parts.map((p) => p.replaceAll("%s", url))
        : [...parts, url];
    }
  }
  if (platform === "darwin") return ["open", url];
  if (platform === "win32") return ["explorer.exe", url];
  if (!env.DISPLAY && !env.WAYLAND_DISPLAY) return null; // no desktop session
  for (const opener of ["xdg-open", "gio", "sensible-browser", "x-www-browser"]) {
    if (which(opener, env, platform)) return opener === "gio" ? [opener, "open", url] : [opener, url];
  }
  return null;
}

export function openBrowser(url: string): Promise<boolean> {
  const command = browserCommand(url);
  if (command === null) return Promise.resolve(false);
  return new Promise((resolve) => {
    try {
      const child = spawn(command[0]!, command.slice(1), { stdio: "ignore", detached: true });
      child.once("error", () => resolve(false));
      child.once("spawn", () => {
        child.unref();
        resolve(true);
      });
    } catch {
      resolve(false);
    }
  });
}
