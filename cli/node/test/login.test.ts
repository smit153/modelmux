import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

import type { Args } from "../src/cli.ts";
import * as login from "../src/commands/login.ts";
import * as logout from "../src/commands/logout.ts";
import { Interrupted, Timeout } from "../src/errors.ts";
import { interruptSignal } from "../src/interrupt.ts";
import { release } from "../src/release.ts";
import { cli, commandWorld, type FakeDocker, FIXTURES, RUNNING, useEnv } from "./helpers.ts";
import { CLAUDE_URL } from "./terminal.test.ts";

const env = useEnv();
let opened: string[] = [];

/** Whether each provider counts as logged in (the helper 'status' answer). */
class Session {
  loggedIn = new Set<string>();

  constructor(docker: FakeDocker) {
    docker.when("run", { returns: [0, "", ""] });
    docker.rules.unshift([(args) => this.isStatus(args), [1, "", ""]]);
  }

  private isStatus(args: readonly string[]): boolean {
    if (args[0] !== "run" || !args.includes("status")) return false;
    const joined = args.join(" ");
    return ![...this.loggedIn].some((name) => joined.includes(`modelmux_${name}-home:`));
  }
}

function setUp(): Session {
  commandWorld(env);
  opened = [];
  login.io.openBrowser = async (url) => {
    opened.push(url);
    return true;
  };
  login.io.stdinIsTTY = () => true;
  return new Session(env.docker);
}

function logInWhenRun(session: Session, name: string): void {
  env.docker.onInteractive = () => session.loggedIn.add(name);
}

const fixture = (name: string): Buffer => readFileSync(path.join(FIXTURES, "login", name));
const tty = { skip: process.platform === "win32" ? "Windows uses the console directly" : false };

describe("login", () => {
  it("opens the browser through the terminal relay, then tests", tty, async () => {
    const session = setUp();
    env.docker.ttyOutput = fixture("claude_browser.raw");
    logInWhenRun(session, "claude");
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 0, all);
    assert.equal(env.docker.ttyCalls.length, 1);
    const args = env.docker.ttyCalls[0]!;
    assert.deepEqual(args.slice(0, 4), ["run", "--rm", "-it", "--name"]);
    assert.ok(args[4]!.startsWith("modelmux-login-claude-"));
    assert.equal(args[args.indexOf("--entrypoint") + 1], "claude");
    assert.deepEqual(args.slice(-2), ["auth", "login"]);
    assert.ok(args.includes("--read-only"));
    assert.ok(args.includes("modelmux_claude-home:/home/modelmux/driver-home"));
    assert.deepEqual(opened, [CLAUDE_URL]);
    assert.ok(env.docker.ttyExtra.some((extra) => extra.includes("Opened your browser")));
    const ups = env.docker.called("compose", "up");
    assert.equal(ups.length, 1);
    assert.deepEqual(ups[0]!.slice(-3), ["-d", "--force-recreate", "modelmux-claude"]);
    assert.match(all, /Logged in to Claude Code\./);
    assert.match(all, /Claude Code is logged in and tested\./);
  });

  it("--no-browser", tty, async () => {
    const session = setUp();
    env.docker.ttyOutput = fixture("codex_device.raw");
    logInWhenRun(session, "codex");
    assert.equal((await cli("login", "codex", "--no-browser")).code, 0);
    assert.deepEqual(opened, []);
    assert.ok(env.docker.ttyExtra.some((extra) => extra.includes("Open the link above")));
  });

  it("no browser available", tty, async () => {
    const session = setUp();
    login.io.openBrowser = async () => false;
    env.docker.ttyOutput = fixture("codex_device.raw");
    logInWhenRun(session, "codex");
    assert.equal((await cli("login", "codex")).code, 0);
    assert.ok(env.docker.ttyExtra.some((extra) => extra.includes("Could not open a browser")));
  });

  it("already logged in", async () => {
    const session = setUp();
    session.loggedIn.add("claude");
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 0);
    assert.match(all, /already logged in/);
    assert.deepEqual(env.docker.ttyCalls, []);
    assert.match(all, /logged in and tested/);
  });

  it("already logged in and running", async () => {
    const session = setUp();
    session.loggedIn.add("claude");
    await cli("up");
    env.docker.when("compose", "ps", { returns: [0, RUNNING, ""] });
    env.docker.calls = [];
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 0);
    assert.deepEqual(env.docker.called("compose", "up"), []);
    assert.match(all, /Claude Code is running at http:\/\/127\.0\.0\.1:8101\/v1/);
  });

  it("--force logs in again", tty, async () => {
    const session = setUp();
    session.loggedIn.add("claude");
    assert.equal((await cli("login", "claude", "--force")).code, 0);
    assert.equal(env.docker.ttyCalls.length, 1);
  });

  it("login not completed", tty, async () => {
    setUp();
    env.docker.ttyResult = 1;
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 1);
    assert.match(all, /login did not complete/);
    assert.match(all, /--raw/);
    assert.deepEqual(env.docker.called("compose", "up"), []);
  });

  it("a timeout removes the helper", tty, async () => {
    setUp();
    env.docker.ttyResult = new Timeout();
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 1);
    assert.match(all, /timed out/);
    const name = env.docker.ttyCalls[0]![4]!;
    assert.ok(env.docker.called("rm", "-f", name).length);
  });

  it("Ctrl+C removes the helper", tty, async () => {
    setUp();
    env.docker.ttyResult = new Interrupted();
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 130);
    assert.match(all, /Cancelled\./);
    assert.ok(env.docker.called("rm", "-f", env.docker.ttyCalls[0]![4]!).length);
  });

  it("--raw uses the console directly", async () => {
    const session = setUp();
    logInWhenRun(session, "claude");
    const { code, all } = await cli("login", "claude", "--raw");
    assert.equal(code, 0);
    assert.deepEqual(env.docker.ttyCalls, []);
    assert.equal(env.docker.passthroughCalls.length, 1);
    assert.equal(env.docker.passthroughCalls[0]![2], "-it");
    assert.match(all, /open the link they show in your browser/);
  });

  it("needs an interactive terminal", async () => {
    setUp();
    login.io.stdinIsTTY = () => false;
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 2);
    assert.match(all, /needs an interactive terminal/);
    assert.deepEqual(env.docker.ttyCalls, []);
    assert.deepEqual(env.docker.passthroughCalls, []);
  });

  it("terminal relay rules", () => {
    const raw = { raw: true } as Args;
    const plain = { raw: false } as Args;
    assert.equal(login.useTty(raw), false);
    assert.equal(login.useTty(plain), process.platform !== "win32");
  });

  it("API key login", async () => {
    const session = setUp();
    const prompts: string[] = [];
    login.io.readSecret = async (prompt) => {
      prompts.push(prompt);
      session.loggedIn.add("codex");
      return "sk-proj-SECRETKEYVALUE123456";
    };
    const { code, all } = await cli("login", "codex", "--method", "api-key");
    assert.equal(code, 0);
    assert.deepEqual(prompts, ["OpenAI API key (input is hidden): "]);
    const calls = env.docker.called("run").filter((c) => c.includes("--with-api-key"));
    assert.equal(calls.length, 1);
    assert.equal(calls[0]![2], "-i");
    assert.ok(!calls[0]!.includes("-t"));
    assert.ok(!calls[0]!.join(" ").includes("sk-proj-SECRETKEYVALUE123456"));
    assert.ok(!all.includes("SECRETKEYVALUE"));
    assert.deepEqual(env.docker.ttyCalls, []);
  });

  it("empty API key", async () => {
    setUp();
    login.io.readSecret = async () => "  ";
    const { code, all } = await cli("login", "codex", "--method", "api-key");
    assert.equal(code, 2);
    assert.match(all, /No OpenAI API key entered/);
  });

  it("unknown method", async () => {
    setUp();
    const { code, all } = await cli("login", "claude", "--method", "magic");
    assert.equal(code, 2);
    assert.match(all, /browser \(/);
    assert.match(all, /console \(/);
  });

  it("needs an image", async () => {
    setUp();
    release.pinnedImage = () => null;
    const { code, all } = await cli("login", "claude");
    assert.equal(code, 2);
    assert.match(all, /--image/);
    assert.deepEqual(env.docker.calls, []);
  });
});

describe("logout", () => {
  it("nothing to remove", async () => {
    setUp();
    env.docker.when("volume", "inspect", { returns: [1, "", ""] });
    const { code, all } = await cli("logout", "claude", "--yes");
    assert.equal(code, 0);
    assert.match(all, /nothing to remove/);
    assert.deepEqual(env.docker.called("volume", "rm"), []);
  });

  it("removes only that login", async () => {
    setUp();
    await cli("up");
    env.docker.calls = [];
    const { code, all } = await cli("logout", "claude", "--yes");
    assert.equal(code, 0);
    assert.ok(env.docker.called("compose", "rm", "--stop", "--force", "modelmux-claude").length);
    const logouts = env.docker.called("run").filter((c) => c.slice(-2).join(" ") === "auth logout");
    assert.equal(logouts.length, 1);
    assert.ok(logouts[0]!.includes("modelmux_claude-home:/home/modelmux/driver-home"));
    assert.deepEqual(env.docker.called("volume", "rm"), [["volume", "rm", "modelmux_claude-home"]]);
    assert.match(all, /Logged out of Claude Code/);
  });

  it("still removes the login when the provider's logout fails", async () => {
    setUp();
    env.docker.when("run", { returns: [1, "", "network down"] });
    assert.equal((await cli("logout", "codex", "--yes")).code, 0);
    assert.ok(env.docker.called("volume", "rm", "modelmux_codex-home").length);
  });

  it("asks first", async () => {
    setUp();
    logout.io.stdinIsTTY = () => true;
    const questions: string[] = [];
    logout.io.ask = async (question) => {
      questions.push(question);
      return "n";
    };
    const { code, all } = await cli("logout", "claude");
    assert.equal(code, 0);
    assert.deepEqual(questions, ["Remove the saved Claude Code login? [y/N] "]);
    assert.match(all, /Nothing changed\./);
    assert.deepEqual(env.docker.called("volume", "rm"), []);
    logout.io.ask = async () => "yes";
    assert.equal((await cli("logout", "claude")).code, 0);
    assert.ok(env.docker.called("volume", "rm").length);
  });

  it("needs --yes without a terminal", async () => {
    setUp();
    logout.io.stdinIsTTY = () => false;
    const { code, all } = await cli("logout", "claude");
    assert.equal(code, 2);
    assert.match(all, /--yes/);
    assert.deepEqual(env.docker.called("volume", "rm"), []);
  });
});

describe("termination signals", () => {
  for (const sig of ["SIGTERM", "SIGHUP"] as const) {
    it(`${sig} cancels like Ctrl+C`, { skip: process.platform === "win32" ? "POSIX signals" : false }, async () => {
      const before = process.listenerCount(sig);
      await login.cancelOnSignals(async () => {
        process.kill(process.pid, sig);
        await new Promise((resolve) => setTimeout(resolve, 100));
      });
      assert.ok(interruptSignal().aborted);
      assert.equal(process.listenerCount(sig), before);
    });
  }
});
