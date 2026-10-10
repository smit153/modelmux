/**
 * The ONLY module that runs other programs: the `docker` CLI.
 *
 * - Commands are argument lists, never shell strings.
 * - Every call has a timeout.
 * - Docker's raw output is shown only with --verbose (and redacted there);
 *   failures become `DockerError`s with a plain message and a next step.
 */

import { type ChildProcess, spawn as nodeSpawn, type SpawnOptions } from "node:child_process";
import { accessSync, constants, statSync } from "node:fs";
import path from "node:path";

import type { Console } from "./console.ts";
import { DockerError, Interrupted, Timeout } from "./errors.ts";
import { interruptSignal } from "./interrupt.ts";
import { shellJoin } from "./text.ts";

export const DEFAULT_TIMEOUT = 60.0;
export const PULL_TIMEOUT = 1800.0;

export type Spawn = (command: string, args: readonly string[], options: SpawnOptions) => ChildProcess;

export class Result {
  readonly args: readonly string[];
  readonly returncode: number;
  readonly stdout: string;
  readonly stderr: string;

  constructor(args: readonly string[], returncode: number, stdout: string, stderr: string) {
    this.args = args;
    this.returncode = returncode;
    this.stdout = stdout;
    this.stderr = stderr;
  }

  get ok(): boolean {
    return this.returncode === 0;
  }
}

export interface RunOptions {
  timeout?: number;
  inputText?: string | null;
  check?: boolean;
}

function startDockerHint(platform: string): string {
  if (platform === "darwin" || platform === "win32") {
    return "Start Docker Desktop and wait until it says it is running, then try again.";
  }
  return "Start the Docker service (for example: sudo systemctl start docker), then try again.";
}

function installHint(platform: string): string {
  if (platform === "darwin" || platform === "win32") {
    return "Install Docker Desktop: https://docs.docker.com/desktop/";
  }
  return "Install Docker Engine: https://docs.docker.com/engine/install/";
}

// [pattern in Docker's stderr, message, hint factory]
const CLASSIFIERS: ReadonlyArray<readonly [RegExp, string, (platform: string) => string]> = [
  [
    /permission denied while trying to connect/i,
    "Your user is not allowed to use Docker.",
    () =>
      "Add yourself to the docker group (sudo usermod -aG docker $USER), " +
      "then log out and back in.",
  ],
  [
    /cannot connect to the docker daemon|is the docker daemon running|error during connect|docker_engine.*(cannot find|not found)|failed to connect to the docker API/i,
    "Docker is installed but not running.",
    startDockerHint,
  ],
  [
    /'compose' is not a docker command|unknown command.*compose|unknown shorthand flag: 'f' in -f/i,
    "Docker Compose v2 is not available.",
    () => "Install the Docker Compose plugin: https://docs.docker.com/compose/install/",
  ],
  [
    /port is already allocated|address already in use|bind: .*in use/i,
    "A port ModelMux needs is already in use.",
    () => "Run 'modelmux doctor' to see which port, or choose another with --port.",
  ],
  [
    /manifest unknown|manifest for .* not found|not found: manifest|failed to resolve reference .*: not found/i,
    "The ModelMux image for this version was not found.",
    () =>
      "Check for a newer modelmux-cli (npm install -g modelmux-cli@latest), or run 'modelmux doctor'.",
  ],
  [
    // GHCR answers a bare "denied" for images that do not exist or are private.
    /pull access denied|unauthorized|error from registry: denied|denied: /i,
    "The image registry refused access (the image may not exist or may be private).",
    () =>
      "Check that you can reach ghcr.io and are not logged in with expired " +
      "credentials (docker logout ghcr.io).",
  ],
  [
    /tls handshake timeout|i\/o timeout|no such host|dial tcp|network is unreachable|proxyconnect|connection refused|temporary failure in name resolution/i,
    "Docker could not reach the internet.",
    () =>
      "Check your connection or proxy (HTTPS_PROXY, and Docker Desktop's proxy " +
      "settings), then try again.",
  ],
  [
    /no space left on device/i,
    "Docker has run out of disk space.",
    () => "Free space with 'docker system prune' (it removes unused images), then retry.",
  ],
];

export function classify(stderr: string, command: string, platform: string = process.platform): DockerError {
  for (const [pattern, message, hint] of CLASSIFIERS) {
    if (pattern.test(stderr)) return new DockerError(message, { hint: hint(platform) });
  }
  return new DockerError(`Docker command failed: docker ${command}.`, {
    hint: "Run again with --verbose to see Docker's output.",
  });
}

/** Python's `shutil.which()`: the first executable `name` on PATH, or null. */
export function which(
  name: string,
  env: NodeJS.ProcessEnv = process.env,
  platform: string = process.platform,
): string | null {
  const dirs = (env.PATH ?? env.Path ?? "").split(platform === "win32" ? ";" : ":");
  const exts =
    platform === "win32"
      ? (env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").filter(Boolean)
      : [""];
  for (const dir of dirs) {
    if (!dir) continue;
    for (const ext of exts) {
      const candidate = path.join(dir, name + ext);
      try {
        if (!statSync(candidate).isFile()) continue;
        if (platform !== "win32") accessSync(candidate, constants.X_OK);
        return candidate;
      } catch {
        // not here
      }
    }
  }
  return null;
}

/** Kill a child that is still running. */
function stop(child: ChildProcess): void {
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
}

/** Exit code like Python's subprocess: -N when killed by signal N. */
function exitCode(code: number | null, signal: NodeJS.Signals | null): number {
  if (code !== null) return code;
  const numbers: Record<string, number> = { SIGHUP: 1, SIGINT: 2, SIGKILL: 9, SIGTERM: 15 };
  return signal ? -(numbers[signal] ?? 1) : -1;
}

export interface DockerOptions {
  spawn?: Spawn;
  which?: (name: string) => string | null;
  platform?: string;
}

export interface TtyOptions {
  onOutput: (data: Buffer) => Buffer | null;
  timeout: number;
  stdout?: { write(data: Buffer): unknown };
}

export class Docker {
  readonly console: Console;
  readonly platform: string;
  private readonly spawnFn: Spawn;
  private readonly whichFn: (name: string) => string | null;
  private found: string | null = null;

  constructor(console: Console, options: DockerOptions = {}) {
    this.console = console;
    this.spawnFn = options.spawn ?? nodeSpawn;
    this.whichFn = options.which ?? ((name) => which(name));
    this.platform = options.platform ?? process.platform;
  }

  get binary(): string {
    if (this.found === null) {
      const found = this.whichFn("docker");
      if (found === null) {
        throw new DockerError("Docker is not installed.", { hint: installHint(this.platform) });
      }
      this.found = found;
    }
    return this.found;
  }

  private argv(args: readonly string[]): string[] {
    for (const arg of args) {
      if (typeof arg !== "string" || arg.includes("\x00")) {
        throw new DockerError("Internal error: invalid docker argument.");
      }
    }
    return [this.binary, ...args];
  }

  private spawnError(error: NodeJS.ErrnoException, file: string): DockerError {
    if (error.code === "ENOENT") {
      return new DockerError("Docker is not installed.", { hint: installHint(this.platform) });
    }
    if (error.code === "EACCES" || error.code === "EPERM") {
      return new DockerError("The docker command cannot be executed.", {
        hint: `Check permissions of ${file}.`,
      });
    }
    return new DockerError(`The docker command could not be started (${error.code ?? error.message}).`);
  }

  /**
   * Start `docker <args>` and settle when it exits, times out (`onTimeout`)
   * or is interrupted (`Interrupted`); the child is stopped in both cases.
   */
  private supervise(
    child: ChildProcess,
    file: string,
    timeout: number | null,
    onTimeout: () => Error,
  ): Promise<number> {
    const signal = interruptSignal();
    return new Promise<number>((resolve, reject) => {
      let timer: NodeJS.Timeout | undefined;
      const cleanup = (): void => {
        if (timer !== undefined) clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
      };
      const onAbort = (): void => {
        cleanup();
        stop(child);
        reject(new Interrupted());
      };
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener("abort", onAbort, { once: true });
      if (timeout !== null) {
        timer = setTimeout(() => {
          cleanup();
          stop(child);
          reject(onTimeout());
        }, timeout * 1000);
      }
      child.once("error", (error: NodeJS.ErrnoException) => {
        cleanup();
        reject(this.spawnError(error, file));
      });
      child.once("close", (code, sig) => {
        cleanup();
        resolve(exitCode(code, sig));
      });
    });
  }

  /** Run `docker <args>` and capture its output. */
  async run(args: readonly string[], options: RunOptions = {}): Promise<Result> {
    const timeout = options.timeout ?? DEFAULT_TIMEOUT;
    const check = options.check ?? true;
    const inputText = options.inputText ?? null;
    const [file, ...rest] = this.argv(args);
    this.console.detail("$ docker " + shellJoin(args));
    const child = this.spawnFn(file!, rest, {
      stdio: [inputText === null ? "ignore" : "pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    child.stdout?.on("data", (chunk: Buffer) => out.push(chunk));
    child.stderr?.on("data", (chunk: Buffer) => err.push(chunk));
    if (inputText !== null && child.stdin) {
      child.stdin.on("error", () => undefined); // the program may exit before reading
      child.stdin.end(inputText, "utf8");
    }
    const returncode = await this.supervise(child, file!, timeout, () =>
      new DockerError(`Docker did not finish 'docker ${args[0] ?? ""}' in time.`, {
        hint: "Docker may be busy or stuck; check Docker is healthy and try again.",
      }),
    );
    const result = new Result(
      [...args],
      returncode,
      Buffer.concat(out).toString("utf8"),
      Buffer.concat(err).toString("utf8"),
    );
    if (result.stdout.trim()) this.console.detail(result.stdout.trimEnd());
    if (result.stderr.trim()) this.console.detail(result.stderr.trimEnd());
    if (check && !result.ok) throw classify(result.stderr, args[0] ?? "", this.platform);
    return result;
  }

  /**
   * Run `docker <args>` attached to this terminal (logs -f, interactive login).
   *
   * Output is not captured, so it is the user's own Docker output.
   */
  async passthrough(args: readonly string[], options: { timeout?: number | null } = {}): Promise<number> {
    const [file, ...rest] = this.argv(args);
    this.console.detail("$ docker " + shellJoin(args));
    const child = this.spawnFn(file!, rest, { stdio: "inherit" });
    return this.supervise(child, file!, options.timeout ?? null, () =>
      new DockerError("Docker did not finish in time.", { hint: "Try again." }),
    );
  }

  /**
   * POSIX: run `docker <args>` (which include `-it`) with our terminal as its
   * input, and relay its output.
   *
   * Docker itself puts the terminal in raw mode and gives the program a
   * terminal, so keystrokes go straight from the user to the program without
   * passing through this process. Only the output passes here: it is written
   * to our terminal unchanged and never stored. `onOutput` sees each chunk
   * and may return extra bytes to display (for example "opened your
   * browser"). Rejects with `Timeout` after `timeout` seconds.
   */
  async runTty(args: readonly string[], options: TtyOptions): Promise<number> {
    const [file, ...rest] = this.argv(args);
    this.console.detail("$ docker " + shellJoin(args));
    const sink = options.stdout ?? process.stdout;
    const child = this.spawnFn(file!, rest, { stdio: ["inherit", "pipe", "inherit"] });
    child.stdout?.on("data", (chunk: Buffer) => {
      sink.write(chunk);
      const extra = options.onOutput(chunk);
      if (extra) sink.write(extra);
    });
    return this.supervise(child, file!, options.timeout, () => new Timeout());
  }

  compose(project: string, file: string, args: readonly string[], options: RunOptions = {}): Promise<Result> {
    return this.run(["compose", "-p", project, "-f", file, ...args], options);
  }

  /** Docker is installed, running, and has Compose v2. Returns the server version. */
  async checkAvailable(): Promise<string> {
    const server = (await this.run(["version", "--format", "{{.Server.Version}}"])).stdout.trim();
    await this.run(["compose", "version", "--short"]);
    return server;
  }
}
