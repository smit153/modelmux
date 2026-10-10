import assert from "node:assert/strict";
import type { ChildProcess, SpawnOptions } from "node:child_process";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { beforeEach, describe, it } from "node:test";

import { Console } from "../src/console.ts";
import { classify, Docker, type Spawn } from "../src/docker.ts";
import { DockerError, EXIT_DOCKER } from "../src/errors.ts";
import { clearSecrets, registerSecret } from "../src/redact.ts";
import { Capture } from "./helpers.ts";

interface Call {
  file: string;
  args: readonly string[];
  options: SpawnOptions;
  stdin: string;
}

/** Records spawns; answers with scripted results keyed by the first docker argument. */
class FakeSpawn {
  calls: Call[] = [];
  results = new Map<string, [number, string, string]>();
  error: NodeJS.ErrnoException | null = null;
  hang = false;
  killed: string[] = [];

  spawn: Spawn = (file, args, options) => {
    const call: Call = { file, args, options, stdin: "" };
    this.calls.push(call);
    const child = new EventEmitter() as ChildProcess & EventEmitter;
    const stdout = new PassThrough();
    const stderr = new PassThrough();
    const stdin = new PassThrough();
    stdin.on("data", (d: Buffer) => (call.stdin += d.toString()));
    Object.assign(child, { stdout, stderr, stdin, exitCode: null, signalCode: null });
    child.kill = ((signal: string) => {
      this.killed.push(signal);
      setImmediate(() => child.emit("close", null, signal));
      return true;
    }) as ChildProcess["kill"];
    setImmediate(() => {
      if (this.error) {
        child.emit("error", this.error);
        return;
      }
      if (this.hang) return;
      const [code, out, err] = this.results.get(args[0]!) ?? [0, "", ""];
      stdout.end(out);
      stderr.end(err);
      setImmediate(() => child.emit("close", code, null));
    });
    return child;
  };
}

function make(
  fake: FakeSpawn,
  options: { verbose?: boolean; which?: string | null; platform?: string } = {},
): [Docker, Capture] {
  const err = new Capture();
  const console = new Console({ out: new Capture(), err, verbose: options.verbose ?? false, color: false });
  const docker = new Docker(console, {
    spawn: fake.spawn,
    which: () => (options.which === undefined ? "/usr/bin/docker" : options.which),
    platform: options.platform ?? "linux",
  });
  return [docker, err];
}

const errno = (code: string): NodeJS.ErrnoException => Object.assign(new Error(code), { code });

describe("docker", () => {
  let fake: FakeSpawn;
  beforeEach(() => {
    fake = new FakeSpawn();
    clearSecrets();
  });

  it("runs argument lists, never a shell", async () => {
    fake.results.set("ps", [0, "out", ""]);
    const [docker] = make(fake);
    const result = await docker.run(["ps", "--format", "{{.Names}}"]);
    const call = fake.calls[0]!;
    assert.equal(call.file, "/usr/bin/docker");
    assert.deepEqual(call.args, ["ps", "--format", "{{.Names}}"]);
    assert.ok(!("shell" in call.options));
    assert.deepEqual(call.options.stdio, ["ignore", "pipe", "pipe"]);
    assert.ok(result.ok);
    assert.equal(result.stdout, "out");
  });

  it("sends input text on stdin", async () => {
    const [docker] = make(fake);
    await docker.run(["login"], { inputText: "secret" });
    const call = fake.calls[0]!;
    assert.deepEqual(call.options.stdio, ["pipe", "pipe", "pipe"]);
    assert.equal(call.stdin, "secret");
  });

  it("wraps compose", async () => {
    const [docker] = make(fake);
    await docker.compose("modelmux", "/cfg/compose.yaml", ["up", "-d"]);
    assert.deepEqual(fake.calls[0]!.args, ["compose", "-p", "modelmux", "-f", "/cfg/compose.yaml", "up", "-d"]);
  });

  it("rejects NUL bytes", async () => {
    const [docker] = make(fake);
    await assert.rejects(docker.run(["ps\x00x"]), DockerError);
    assert.deepEqual(fake.calls, []);
  });

  for (const [platform, hint] of [["linux", "Docker Engine"], ["darwin", "Desktop"], ["win32", "Desktop"]]) {
    it(`not installed (${platform})`, async () => {
      const [docker] = make(fake, { which: null, platform });
      await assert.rejects(docker.run(["ps"]), (e: unknown) => {
        assert.ok(e instanceof DockerError);
        assert.match(e.message, /not installed/);
        assert.ok(e.hint!.includes(hint!));
        assert.equal(e.exitCode, EXIT_DOCKER);
        return true;
      });
    });
  }

  for (const [code, message] of [["ENOENT", "not installed"], ["EACCES", "cannot be executed"]]) {
    it(`spawn error ${code}`, async () => {
      fake.error = errno(code!);
      const [docker] = make(fake);
      await assert.rejects(docker.run(["ps"]), (e: unknown) => e instanceof DockerError && e.message.includes(message!));
    });
  }

  it("times out and stops the program", async () => {
    fake.hang = true;
    const [docker] = make(fake);
    await assert.rejects(
      docker.run(["ps"], { timeout: 0.01 }),
      (e: unknown) => e instanceof DockerError && /did not finish 'docker ps' in time/.test(e.message),
    );
    assert.deepEqual(fake.killed, ["SIGTERM"]);
  });

  const cases: Array<[string, string]> = [
    ["Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?", "not running"],
    ["error during connect: open //./pipe/docker_engine: The system cannot find the file specified.", "not running"],
    ["permission denied while trying to connect to the Docker daemon socket", "not allowed"],
    ["docker: 'compose' is not a docker command.", "Compose v2"],
    ["Bind for 0.0.0.0:8101 failed: port is already allocated", "already in use"],
    ["manifest unknown", "was not found"],
    // Real Docker 29 output:
    ['Error response from daemon: failed to resolve reference "docker.io/library/python:0.0.0-nope": docker.io/library/python:0.0.0-nope: not found', "was not found"],
    ["Error response from daemon: error from registry: denied\ndenied", "refused access"],
    ["pull access denied for x, repository does not exist", "refused access"],
    ['Get "https://ghcr.io/v2/": dial tcp: lookup ghcr.io: no such host', "internet"],
    ["net/http: TLS handshake timeout", "internet"],
    ["no space left on device", "disk space"],
    ["something else entirely", "Docker command failed: docker pull"],
  ];
  for (const [stderr, message] of cases) {
    it(`classifies "${stderr.slice(0, 40)}"`, () => {
      const error = classify(stderr, "pull", "linux");
      assert.ok(error.message.includes(message), error.message);
      assert.ok(error.hint);
    });
  }

  it("gives a daemon hint per platform", () => {
    const stderr = "Cannot connect to the Docker daemon";
    assert.match(classify(stderr, "ps", "linux").hint!, /systemctl/);
    assert.match(classify(stderr, "ps", "darwin").hint!, /Docker Desktop/);
  });

  it("hides raw output on failure by default", async () => {
    fake.results.set("pull", [1, "", "weird internal error RAW-DETAIL"]);
    const [docker, err] = make(fake);
    await assert.rejects(docker.run(["pull", "x"]), (e: unknown) => e instanceof DockerError && !e.message.includes("RAW-DETAIL"));
    assert.ok(!err.text.includes("RAW-DETAIL"));
  });

  it("shows commands and output with --verbose, redacted", async () => {
    registerSecret("server-api-key-value-123456");
    fake.results.set("inspect", [0, "token server-api-key-value-123456", ""]);
    const [docker, err] = make(fake, { verbose: true });
    await docker.run(["inspect", "a b"]);
    assert.ok(err.text.includes("$ docker inspect 'a b'"));
    assert.ok(!err.text.includes("server-api-key-value"));
  });

  it("can report failure without raising", async () => {
    fake.results.set("volume", [1, "", "No such volume"]);
    const [docker] = make(fake);
    assert.equal((await docker.run(["volume", "inspect", "x"], { check: false })).returncode, 1);
  });

  it("checks Docker and Compose", async () => {
    fake.results.set("version", [0, "29.8.0\n", ""]);
    const [docker] = make(fake);
    assert.equal(await docker.checkAvailable(), "29.8.0");
    assert.deepEqual(fake.calls[1]!.args, ["compose", "version", "--short"]);
  });

  it("resolves the binary once", async () => {
    const lookups: string[] = [];
    const docker = new Docker(new Console({ out: new Capture(), err: new Capture() }), {
      spawn: fake.spawn,
      which: (name) => {
        lookups.push(name);
        return "/bin/docker";
      },
    });
    await docker.run(["ps"]);
    await docker.run(["ps"]);
    assert.deepEqual(lookups, ["docker"]);
  });
});
