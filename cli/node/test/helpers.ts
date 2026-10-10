/** Shared test helpers: a scripted fake Docker and an isolated CLI home (like conftest.py). */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach } from "node:test";

import * as login from "../src/commands/login.ts";
import * as logout from "../src/commands/logout.ts";
import * as up from "../src/commands/up.ts";
import { factory } from "../src/context.ts";
import { type Docker, Result, type RunOptions, type TtyOptions } from "../src/docker.ts";
import { DockerError } from "../src/errors.ts";
import { server } from "../src/health.ts";
import { resetInterrupt } from "../src/interrupt.ts";
import { main } from "../src/main.ts";
import { ports } from "../src/ports.ts";
import { clearSecrets } from "../src/redact.ts";
import { release, resetReleaseCache } from "../src/release.ts";
import { registry } from "../src/updates.ts";

export const FIXTURES = path.join(import.meta.dirname, "fixtures");
export const PINNED = "ghcr.io/smit153/modelmux@sha256:" + "a".repeat(64);
export const RUNNING = '{"Service": "modelmux-claude", "State": "running", "Health": "", "ExitCode": 0}';
export const EXITED = '{"Service": "modelmux-claude", "State": "exited", "Health": "", "ExitCode": 1}';

export type Response = readonly [number, string, string] | DockerError;
export type Matcher = (args: readonly string[]) => boolean;

/** Does `args` start with `prefix`? */
export function startsWith(args: readonly string[], prefix: readonly string[]): boolean {
  return prefix.every((part, i) => args[i] === part);
}

/**
 * Records every docker call; answers from rules matched in order.
 *
 * `when(...prefix)` matches calls whose arguments start with `prefix`
 * (`compose` calls are matched after `compose -p <project> -f <file>`).
 */
export class FakeDocker {
  calls: string[][] = [];
  passthroughCalls: string[][] = [];
  rules: Array<[Matcher, Response]> = [];
  serverVersion = "29.8.0";
  ttyCalls: string[][] = [];
  ttyOutput: Buffer = Buffer.alloc(0);
  ttyResult: number | Error = 0;
  ttyExtra: Buffer[] = [];
  onInteractive: () => void = () => undefined;
  passthroughResult: number | Error = 0;
  checkAvailableError: DockerError | null = null;

  when(...args: [...string[], { returns: Response }] | string[]): this {
    const last = args[args.length - 1];
    const hasReturns = typeof last === "object" && last !== null;
    const prefix = (hasReturns ? args.slice(0, -1) : args) as string[];
    const returns: Response = hasReturns ? (last as { returns: Response }).returns : [0, "", ""];
    this.rules.unshift([(a) => startsWith(a, prefix), returns]);
    return this;
  }

  private answer(args: string[], key: string[], check: boolean): Result {
    this.calls.push(args);
    let response: Response = [0, "", ""];
    for (const [matcher, value] of this.rules) {
      if (matcher(key)) {
        response = value;
        break;
      }
    }
    if (response instanceof DockerError) throw response;
    const [code, out, err] = response;
    if (check && code !== 0) throw new DockerError(`docker ${args[0]} failed`, { hint: "fake" });
    return new Result(args, code, out, err);
  }

  async run(args: readonly string[], options: RunOptions = {}): Promise<Result> {
    return this.answer([...args], [...args], options.check ?? true);
  }

  async compose(project: string, file: string, args: readonly string[], options: RunOptions = {}): Promise<Result> {
    const full = ["compose", "-p", project, "-f", file, ...args];
    return this.answer(full, ["compose", ...args], options.check ?? true);
  }

  async passthrough(args: readonly string[]): Promise<number> {
    this.passthroughCalls.push([...args]);
    if (this.passthroughResult instanceof Error) throw this.passthroughResult;
    this.onInteractive();
    return this.passthroughResult;
  }

  /** Replays `ttyOutput` byte by byte through `onOutput`. */
  async runTty(args: readonly string[], options: TtyOptions): Promise<number> {
    this.ttyCalls.push([...args]);
    for (let i = 0; i < this.ttyOutput.length; i++) {
      const extra = await options.onOutput(this.ttyOutput.subarray(i, i + 1));
      if (extra) this.ttyExtra.push(extra);
    }
    if (this.ttyResult instanceof Error) throw this.ttyResult;
    this.onInteractive();
    return this.ttyResult;
  }

  async checkAvailable(): Promise<string> {
    this.calls.push(["version"]);
    if (this.checkAvailableError) throw this.checkAvailableError;
    return this.serverVersion;
  }

  /** Calls whose arguments (compose: after the project/file flags) start with prefix. */
  called(...prefix: string[]): string[][] {
    return this.calls.filter((call) => {
      const key = call[0] === "compose" ? ["compose", ...call.slice(5)] : call;
      return startsWith(key, prefix);
    });
  }
}

/** Collects what is written to it. */
export class Capture {
  text = "";
  isTTY = false;

  write(data: string | Buffer): boolean {
    this.text += data.toString();
    return true;
  }
}

export interface CliResult {
  code: number;
  out: string;
  err: string;
  /** stdout + stderr, like capsys in the Python tests. */
  all: string;
}

export async function cli(...argv: string[]): Promise<CliResult> {
  const out = new Capture();
  const err = new Capture();
  const code = await main(argv, { out, err });
  return { code, out: out.text, err: err.text, all: out.text + err.text };
}

type Saved = Array<[Record<string, unknown>, Record<string, unknown>]>;

function snapshot(...objects: object[]): Saved {
  return objects.map((o) => [o as Record<string, unknown>, { ...o }]);
}

function restore(saved: Saved): void {
  for (const [target, values] of saved) Object.assign(target, values);
}

export interface Env {
  docker: FakeDocker;
  home: string;
}

/**
 * Register per-test setup: a fresh fake Docker, an isolated CLI home
 * (MODELMUX_CLI_HOME), no registered secrets, and every replaced function
 * restored afterwards. Returns a live view of the current test's state.
 */
export function useEnv(): Env {
  const env = {} as Env;
  let saved: Saved = [];
  let previousHome: string | undefined;
  beforeEach(() => {
    saved = snapshot(factory, server, ports, release, registry, up.timing, login.io, logout.io);
    env.docker = new FakeDocker();
    factory.makeDocker = () => env.docker as unknown as Docker;
    env.home = path.join(mkdtempSync(path.join(tmpdir(), "modelmux-test-")), "home");
    previousHome = process.env.MODELMUX_CLI_HOME;
    process.env.MODELMUX_CLI_HOME = env.home;
    clearSecrets();
    resetReleaseCache();
    resetInterrupt();
  });
  afterEach(() => {
    restore(saved);
    if (previousHome === undefined) delete process.env.MODELMUX_CLI_HOME;
    else process.env.MODELMUX_CLI_HOME = previousHome;
    rmSync(path.dirname(env.home), { recursive: true, force: true });
  });
  return env;
}

/** The usual command-test world: pinned image, free ports, ready servers, existing volumes/image. */
export function commandWorld(env: Env): void {
  release.pinnedImage = () => PINNED;
  ports.portFree = async () => true;
  up.timing.pollInterval = 0;
  server.ready = async () => true;
  env.docker.when("volume", "inspect", { returns: [0, "[]", ""] });
  env.docker.when("image", "inspect", { returns: [0, "[]", ""] });
}

/** Make the helper 'status' command succeed for these providers only. */
export function loggedIn(docker: FakeDocker, ...names: string[]): void {
  const volumes = names.map((name) => `modelmux_${name}-home:`);
  docker.when("run", { returns: [1, "", ""] });
  docker.rules.unshift([
    (args) => args[0] === "run" && volumes.some((v) => args.join(" ").includes(v)),
    [0, "", ""],
  ]);
}
