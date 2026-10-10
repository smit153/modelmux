/** Talk to a running ModelMux server over HTTP (localhost only). */

import http from "node:http";

import { interruptible, throwIfInterrupted } from "./interrupt.ts";

export const LOCALHOST = "127.0.0.1";

export function baseUrl(port: number): string {
  return `http://${LOCALHOST}:${port}`;
}

/** GET `path` on the local server. Resolves to [status, body]; status 0 if unreachable. */
export function getJson(
  port: number,
  urlPath: string,
  options: { timeout?: number; apiKey?: string | null } = {},
): Promise<[number, unknown]> {
  return interruptible(request(port, urlPath, options));
}

function request(
  port: number,
  urlPath: string,
  options: { timeout?: number; apiKey?: string | null } = {},
): Promise<[number, unknown]> {
  const timeout = options.timeout ?? 3.0;
  const headers: Record<string, string> = {};
  if (options.apiKey != null) headers.Authorization = `Bearer ${options.apiKey}`;
  return new Promise((resolve) => {
    let settled = false;
    const done = (value: [number, unknown]): void => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const request = http.get(
      { host: LOCALHOST, port, path: urlPath, headers, agent: false },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("error", () => done([0, null]));
        response.on("end", () => {
          const status = response.statusCode ?? 0;
          const text = Buffer.concat(chunks).toString("utf8");
          let body: unknown = null;
          try {
            body = text ? JSON.parse(text) : null;
          } catch {
            // urllib: a 2xx with a broken body is "unreachable"; an error status keeps its code.
            done(status >= 200 && status < 300 ? [0, null] : [status, null]);
            return;
          }
          done([status, body]);
        });
      },
    );
    request.setTimeout(timeout * 1000, () => request.destroy(new Error("timeout")));
    request.on("error", () => done([0, null]));
  });
}

export async function ready(port: number): Promise<boolean> {
  const [status, body] = await getJson(port, "/health/ready");
  return (
    status === 200 &&
    typeof body === "object" &&
    body !== null &&
    !Array.isArray(body) &&
    (body as Record<string, unknown>).status === "ready"
  );
}

/** Sleep; Ctrl+C ends it early with `Interrupted`. */
export const sleep = (seconds: number): Promise<void> =>
  interruptible(new Promise((resolve) => setTimeout(resolve, seconds * 1000)));

export interface WaitOptions {
  timeout: number;
  interval?: number;
  shouldStop?: () => boolean | Promise<boolean>;
  sleep?: (seconds: number) => Promise<void>;
  clock?: () => number;
}

/** Poll `check` until it is true (true), `shouldStop` is true or time runs out (false). */
export async function waitUntil(
  check: () => boolean | Promise<boolean>,
  options: WaitOptions,
): Promise<boolean> {
  const interval = options.interval ?? 1.0;
  const shouldStop = options.shouldStop ?? (() => false);
  const pause = options.sleep ?? sleep;
  const clock = options.clock ?? (() => performance.now() / 1000);
  const deadline = clock() + options.timeout;
  for (;;) {
    throwIfInterrupted();
    if (await check()) return true;
    if ((await shouldStop()) || clock() >= deadline) return false;
    await pause(interval);
  }
}
