/**
 * Network checks used only by `doctor` and `upgrade` (never in the background).
 *
 * Node honours the usual proxy variables (HTTPS_PROXY / NO_PROXY) for these
 * requests when NODE_USE_ENV_PROXY=1 is set.
 */

export const PACKAGE = "modelmux-cli";
export const NPM_URL = `https://registry.npmjs.org/${PACKAGE}/latest`;
export const REGISTRY_URL = "https://ghcr.io/v2/";
export const UPGRADE_HINT = `npm install -g ${PACKAGE}@latest   (or: npx ${PACKAGE}@latest)`;
const RELEASE = /^\d+(\.\d+)*$/;

/** Plain releases only ("1.2.3"); pre-releases and build metadata are ignored. */
export function parseVersion(value: string): number[] | null {
  return RELEASE.test(value) ? value.split(".").map(Number) : null;
}

/** Python's tuple comparison: element by element, then the longer one wins. */
function compare(a: readonly number[], b: readonly number[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i]! - b[i]!;
  }
  return a.length - b.length;
}

/** The newest modelmux-cli on npm, or null if it cannot be checked. */
export async function latestVersion(timeout = 5.0): Promise<string | null> {
  try {
    const response = await fetch(NPM_URL, { signal: AbortSignal.timeout(timeout * 1000) });
    if (!response.ok) return null;
    const data: unknown = await response.json();
    const version =
      typeof data === "object" && data !== null ? (data as Record<string, unknown>).version : null;
    return typeof version === "string" ? version : null;
  } catch {
    return null;
  }
}

export function newerAvailable(current: string, latest: string | null): boolean {
  if (latest === null) return false;
  const now = parseVersion(current);
  const next = parseVersion(latest);
  return now !== null && next !== null && compare(next, now) > 0;
}

/** Can we reach the image registry? Any HTTP answer (even 401) counts. */
export async function registryReachable(timeout = 5.0): Promise<boolean> {
  try {
    await fetch(REGISTRY_URL, { signal: AbortSignal.timeout(timeout * 1000) });
    return true;
  } catch {
    return false;
  }
}
