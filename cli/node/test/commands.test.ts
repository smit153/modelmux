import assert from "node:assert/strict";
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

import * as up from "../src/commands/up.ts";
import { server } from "../src/health.ts";
import { ports } from "../src/ports.ts";
import { release } from "../src/release.ts";
import { Stack } from "../src/stack.ts";
import { cli, commandWorld, EXITED, loggedIn, RUNNING, useEnv } from "./helpers.ts";

const posix = { skip: process.platform === "win32" ? "POSIX permissions" : false };
const env = useEnv();

function setUp(): void {
  commandWorld(env);
}

describe("up", () => {
  it("needs an image in a dev build", async () => {
    setUp();
    release.pinnedImage = () => null;
    const { code, all } = await cli("up");
    assert.equal(code, 2);
    assert.match(all, /--image/);
    assert.deepEqual(env.docker.calls, []);
  });

  it("prepares everything when nothing is logged in", async () => {
    setUp();
    loggedIn(env.docker);
    const { code, all } = await cli("up");
    assert.equal(code, 0);
    assert.match(all, /No provider is logged in yet/);
    assert.match(all, /modelmux login claude/);
    assert.deepEqual(env.docker.called("compose", "up"), []);
    assert.ok(existsSync(path.join(env.home, "compose.yaml")));
    assert.ok(existsSync(path.join(env.home, "secrets.env")));
    assert.match(all, /Created an API key/);
    const key = readFileSync(path.join(env.home, "secrets.env"), "utf8").split("=")[1]!.trim();
    assert.ok(!all.includes(key));
  });

  it("starts logged-in providers", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    const { code, all } = await cli("up");
    assert.equal(code, 0);
    const calls = env.docker.called("compose", "up");
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0]!.slice(-3), ["up", "-d", "modelmux-claude"]);
    assert.match(all, /Claude Code is running at http:\/\/127\.0\.0\.1:8101\/v1/);
    assert.match(all, /OpenAI Codex is not logged in: modelmux login codex/);
  });

  it("is idempotent", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    await cli("up");
    env.docker.when("compose", "ps", { returns: [0, RUNNING, ""] });
    const { code, all } = await cli("up");
    assert.equal(code, 0);
    assert.match(all, /already running/);
    assert.doesNotMatch(all, /Created an API key/);
  });

  it("skips the login check and compose for a running provider", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    await cli("up", "claude");
    env.docker.calls = [];
    env.docker.when("compose", "ps", { returns: [0, RUNNING, ""] });
    const { code, all } = await cli("up", "claude");
    assert.equal(code, 0);
    assert.match(all, /already running/);
    assert.deepEqual(env.docker.calls.filter((c) => c[0] === "run" && c[1] === "--rm"), []);
    assert.deepEqual(env.docker.called("compose", "up"), []);
  });

  it("recreates a running provider when settings change", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    await cli("up", "claude");
    env.docker.when("compose", "ps", { returns: [0, RUNNING, ""] });
    const { code, all } = await cli("up", "claude", "--port", "claude=9101");
    assert.equal(code, 0);
    assert.ok(env.docker.called("compose", "up").length);
    assert.match(all, /running at http:\/\/127\.0\.0\.1:9101\/v1/);
  });

  it("checks logins in parallel", async (t) => {
    setUp();
    let active = 0;
    let peak = 0;
    t.mock.method(Stack.prototype, "loggedIn", async () => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise((resolve) => setTimeout(resolve, 50));
      active -= 1;
      return false;
    });
    const { code } = await cli("up");
    assert.equal(code, 0);
    assert.equal(peak, 2); // claude and codex checked at the same time
  });

  it("refuses an explicit provider that is not logged in", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    const { code, all } = await cli("up", "codex");
    assert.equal(code, 1);
    assert.match(all, /OpenAI Codex is not logged in yet\./);
    assert.match(all, /modelmux login codex/);
    assert.deepEqual(env.docker.called("compose", "up"), []);
  });

  it("rejects an unknown provider", async () => {
    setUp();
    const { code, all } = await cli("up", "gemini");
    assert.equal(code, 2);
    assert.match(all, /Unknown provider 'gemini'/);
  });

  it("reports a port in use", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    ports.portFree = async (port) => port !== 8101;
    const { code, all } = await cli("up");
    assert.equal(code, 1);
    assert.match(all, /Port 8101 for Claude Code is already in use/);
    assert.match(all, /--port claude=<port>/);
    assert.deepEqual(env.docker.called("compose", "up"), []);
  });

  it("remembers the port and image", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    const { code, all } = await cli("up", "--port", "claude=9101", "--image", "modelmux:dev");
    assert.equal(code, 0);
    assert.match(all, /127\.0\.0\.1:9101/);
    const config = JSON.parse(readFileSync(path.join(env.home, "config.json"), "utf8"));
    assert.deepEqual(config, { version: 1, ports: { claude: 9101 }, image: "modelmux:dev" });
    const text = readFileSync(path.join(env.home, "compose.yaml"), "utf8");
    const compose = JSON.parse(text.slice(text.indexOf("\n") + 1));
    assert.equal(compose.services["modelmux-claude"].image, "modelmux:dev");
  });

  for (const value of ["claude", "claude=80", "claude=abc", "gemini=9000", "=9000", "claude=70000"]) {
    it(`rejects --port ${value}`, async () => {
      setUp();
      const { code, all } = await cli("up", "--port", value);
      assert.equal(code, 2);
      assert.match(all, /Invalid --port/);
    });
  }

  it("rejects duplicate ports", async () => {
    setUp();
    const { code, all } = await cli("up", "--port", "claude=8102");
    assert.equal(code, 2);
    assert.match(all, /same port/);
  });

  it("reports a container that stops while starting", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    server.ready = async () => false;
    env.docker.when("compose", "ps", { returns: [0, EXITED, ""] });
    const { code, all } = await cli("up");
    assert.equal(code, 1);
    assert.match(all, /stopped while starting/);
    assert.match(all, /modelmux login claude/);
  });

  it("reports a server that is not ready in time", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    server.ready = async () => false;
    up.timing.readyTimeout = 0;
    const { code, all } = await cli("up");
    assert.equal(code, 1);
    assert.match(all, /did not become ready in time/);
    assert.match(all, /modelmux logs claude/);
  });

  it("pulls a missing image", async () => {
    setUp();
    loggedIn(env.docker);
    env.docker.when("image", "inspect", { returns: [1, "", ""] });
    const { code, all } = await cli("up");
    assert.equal(code, 0);
    assert.match(all, /Downloading the ModelMux server image/);
    assert.ok(env.docker.called("pull").length);
  });

  it("keeps its files private", posix, async () => {
    setUp();
    await cli("up");
    for (const name of ["compose.yaml", "secrets.env"]) {
      assert.equal(statSync(path.join(env.home, name)).mode & 0o777, 0o600);
    }
    assert.equal(statSync(env.home).mode & 0o777, 0o700);
  });
});

describe("down / logs / status", () => {
  it("down when not set up", async () => {
    setUp();
    const { code, all } = await cli("down");
    assert.equal(code, 0);
    assert.match(all, /not running/);
    assert.deepEqual(env.docker.calls, []);
  });

  it("down keeps logins", async () => {
    setUp();
    await cli("up");
    const { code, all } = await cli("down");
    assert.equal(code, 0);
    const calls = env.docker.called("compose", "down");
    assert.equal(calls.length, 1);
    assert.ok(!calls[0]!.includes("-v"));
    assert.ok(!calls[0]!.includes("--volumes"));
    assert.match(all, /logins are kept/);
  });

  it("logs when not set up", async () => {
    setUp();
    const { code, all } = await cli("logs");
    assert.equal(code, 1);
    assert.match(all, /not set up/);
  });

  it("logs pass through", async () => {
    setUp();
    await cli("up");
    assert.equal((await cli("logs", "claude", "-f", "--tail", "5")).code, 0);
    assert.equal(env.docker.passthroughCalls.length, 1);
    const args = env.docker.passthroughCalls[0]!;
    assert.deepEqual(args.slice(0, 3), ["compose", "-p", "modelmux"]);
    assert.deepEqual(args.slice(5), ["logs", "--no-log-prefix", "--tail", "5", "--follow", "modelmux-claude"]);
  });

  it("status table", async () => {
    setUp();
    loggedIn(env.docker, "claude");
    await cli("up");
    env.docker.when("compose", "ps", { returns: [0, RUNNING, ""] });
    const { code, all } = await cli("status");
    assert.equal(code, 0);
    const lines = all.split("\n").map((line) => line.trim().split(/\s+/));
    assert.deepEqual(lines[0], ["PROVIDER", "CONTAINER", "LOGIN", "HEALTH", "URL"]);
    assert.deepEqual(lines[1], ["claude", "running", "yes", "ready", "http://127.0.0.1:8101/v1"]);
    assert.deepEqual(lines[2], ["codex", "not", "started", "no", "-", "-"]);
  });

  it("status reports a stopped container", async () => {
    setUp();
    await cli("up");
    env.docker.when("compose", "ps", { returns: [0, EXITED, ""] });
    const { code, all } = await cli("status");
    assert.equal(code, 0);
    assert.match(all, /Claude Code has stopped/);
    assert.match(all, /modelmux login claude/);
  });
});

