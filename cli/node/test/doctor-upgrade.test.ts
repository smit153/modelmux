import assert from "node:assert/strict";
import { chmodSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

import { DockerError } from "../src/errors.ts";
import { server } from "../src/health.ts";
import { ports } from "../src/ports.ts";
import * as updates from "../src/updates.ts";
import { VERSION } from "../src/version.ts";
import { cli, commandWorld, RUNNING, useEnv } from "./helpers.ts";

const EXITED = '{"Service": "modelmux-claude", "State": "exited", "Health": "", "ExitCode": 3}';
const env = useEnv();

function setUp(): void {
  commandWorld(env);
  server.getJson = async () => [200, { data: [] }];
  updates.registry.registryReachable = async () => true;
  updates.registry.latestVersion = async () => VERSION;
  // Claude logged in, Codex not.
  env.docker.when("run", { returns: [1, "", ""] });
  env.docker.rules.unshift([
    (a) => a[0] === "run" && a.includes("modelmux_claude-home:/home/modelmux/driver-home"),
    [0, "", ""],
  ]);
}

async function setUpStack(ps: string = RUNNING): Promise<void> {
  assert.equal((await cli("up")).code, 0);
  env.docker.when("compose", "ps", { returns: [0, ps, ""] });
}

describe("doctor", () => {
  it("all good", async () => {
    setUp();
    await setUpStack();
    const { code, all } = await cli("doctor");
    assert.equal(code, 0);
    assert.match(all, /Docker 29\.8\.0 is running/);
    assert.match(all, /Claude Code: logged in, running and answering/);
    assert.match(all, /OpenAI Codex: not logged in/);
    assert.match(all, /No problems/);
  });

  it("Docker not running", async () => {
    setUp();
    env.docker.checkAvailableError = new DockerError("Docker is installed but not running.", {
      hint: "Start Docker Desktop.",
    });
    const { code, all } = await cli("doctor");
    assert.equal(code, 1);
    assert.match(all, /not running/);
    assert.match(all, /Start Docker Desktop\./);
  });

  it("not set up", async () => {
    setUp();
    const { code, all } = await cli("doctor");
    assert.equal(code, 1);
    assert.match(all, /not set up/);
  });

  it("offline, and a newer CLI exists", async () => {
    setUp();
    await setUpStack();
    updates.registry.registryReachable = async () => false;
    updates.registry.latestVersion = async () => "99.0.0";
    const { code, all } = await cli("doctor");
    assert.equal(code, 0); // notes, not failures
    assert.match(all, /Cannot reach ghcr\.io/);
    assert.match(all, /HTTPS_PROXY/);
    assert.match(all, /modelmux-cli 99\.0\.0 is available/);
    assert.match(all, /npm install -g modelmux-cli@latest/);
  });

  const cases: Array<[string | null, string]> = [
    ["live check failed: auth", "modelmux login claude --force"],
    ["CLI version 3.0.0 is not in >=2.1,<3", "modelmux upgrade"],
    ["CLI does not support lockdown flag --x", "modelmux upgrade"],
    [null, "modelmux logs claude"],
  ];
  for (const [reason, hint] of cases) {
    it(`explains a stopped server (${reason ?? "no reason"})`, async () => {
      setUp();
      await setUpStack(EXITED);
      const logs = ['{"event": "startup"}', "not json"];
      if (reason) logs.push(JSON.stringify({ event: "probe_failed", reason }));
      env.docker.when("compose", "logs", { returns: [0, logs.join("\n"), ""] });
      const { code, all } = await cli("doctor");
      assert.equal(code, 1);
      assert.match(all, /Claude Code: the server stopped/);
      if (reason) assert.ok(all.includes(reason));
      assert.ok(all.includes(hint));
    });
  }

  it("wrong API key", async () => {
    setUp();
    await setUpStack();
    server.getJson = async () => [401, null];
    const { code, all } = await cli("doctor");
    assert.equal(code, 1);
    assert.match(all, /does not accept the saved API key/);
  });

  it("port conflict", async () => {
    setUp();
    await setUpStack("");
    ports.portFree = async (port) => port !== 8101;
    const { code, all } = await cli("doctor");
    assert.equal(code, 1);
    assert.match(all, /port 8101 is used by another program/);
  });

  it("missing image", async () => {
    setUp();
    await setUpStack();
    env.docker.when("image", "inspect", { returns: [1, "", ""] });
    assert.match((await cli("doctor")).all, /is not downloaded yet/);
  });

  it("loose permissions", { skip: process.platform === "win32" ? "POSIX permissions" : false }, async () => {
    setUp();
    await setUpStack();
    chmodSync(path.join(env.home, "secrets.env"), 0o644);
    const { code, all } = await cli("doctor");
    assert.equal(code, 1);
    assert.match(all, /readable by other users/);
    assert.match(all, /chmod 600/);
  });
});

describe("upgrade", () => {
  it("recreates running providers", async () => {
    setUp();
    await setUpStack();
    env.docker.calls = [];
    const { code, all } = await cli("upgrade");
    assert.equal(code, 0);
    const ups = env.docker.called("compose", "up");
    assert.equal(ups.length, 1);
    assert.equal(ups[0]!.at(-1), "modelmux-claude");
    assert.match(all, /Logins were kept\./);
    assert.deepEqual(env.docker.called("volume", "rm"), []);
  });

  it("with a new image", async () => {
    setUp();
    await setUpStack();
    const { code, all } = await cli("upgrade", "--image", "ghcr.io/smit153/modelmux:9.9.9");
    assert.equal(code, 0);
    assert.match(all, /Server image: ghcr\.io\/smit153\/modelmux:9\.9\.9/);
    const config = JSON.parse(readFileSync(path.join(env.home, "config.json"), "utf8"));
    assert.ok(config.image.endsWith(":9.9.9"));
    const text = readFileSync(path.join(env.home, "compose.yaml"), "utf8");
    const compose = JSON.parse(text.slice(text.indexOf("\n") + 1));
    assert.ok(compose.services["modelmux-claude"].image.endsWith(":9.9.9"));
  });

  it("nothing running", async () => {
    setUp();
    const { code, all } = await cli("upgrade");
    assert.equal(code, 0);
    assert.match(all, /Nothing was running/);
    assert.deepEqual(env.docker.called("compose", "up"), []);
  });

  it("mentions a newer CLI", async () => {
    setUp();
    updates.registry.latestVersion = async () => "99.0.0";
    assert.match((await cli("upgrade")).all, /modelmux-cli 99\.0\.0 is available/);
  });
});

describe("update checks", () => {
  const cases: Array<[string, string | null, boolean]> = [
    ["0.1.0", "0.2.0", true], ["0.1.0", "0.1.0", false], ["0.2.0", "0.1.9", false],
    ["0.1.0", "0.2.0-rc.1", false], ["0.1.0", null, false], ["0.1.0", "1.0", true],
    ["0.1.0", "0.1.0.1", true], ["0.10.0", "0.9.0", false],
  ];
  for (const [current, latest, newer] of cases) {
    it(`newerAvailable(${current}, ${latest}) is ${newer}`, () => {
      assert.equal(updates.newerAvailable(current, latest), newer);
    });
  }

  it("reads the latest version from npm", async (t) => {
    const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({ version: "1.2.3" }));
    assert.equal(await updates.registry.latestVersion(), "1.2.3");
    assert.equal(fetch.mock.calls[0]!.arguments[0], "https://registry.npmjs.org/modelmux-cli/latest");
  });

  for (const [name, impl] of [
    ["offline", async () => { throw new TypeError("fetch failed"); }],
    ["bad JSON", async () => new Response("{nope")],
    ["HTTP 404", async () => new Response("{}", { status: 404 })],
  ] as const) {
    it(`latest version is unknown when ${name}`, async (t) => {
      t.mock.method(globalThis, "fetch", impl);
      assert.equal(await updates.registry.latestVersion(), null);
    });
  }

  it("registry unreachable", async (t) => {
    t.mock.method(globalThis, "fetch", async () => {
      throw new TypeError("fetch failed");
    });
    assert.equal(await updates.registry.registryReachable(), false);
  });

  it("registry reachable on an HTTP error", async (t) => {
    t.mock.method(globalThis, "fetch", async () => new Response("", { status: 401 }));
    assert.equal(await updates.registry.registryReachable(), true);
  });
});
