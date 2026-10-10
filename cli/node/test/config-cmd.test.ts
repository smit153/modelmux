import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

import { loadTemplate, render, TARGETS, type Target } from "../src/clientconfig.ts";
import { CliError } from "../src/errors.ts";
import { server } from "../src/health.ts";
import { sharedDir } from "../src/providers.ts";
import { cli, commandWorld, useEnv } from "./helpers.ts";

const TARGETS_LIST: Target[] = [
  { provider: "claude", displayName: "Claude Code", baseUrl: "http://127.0.0.1:8101/v1", models: ["sonnet", "opus"] },
  { provider: "codex", displayName: "OpenAI Codex", baseUrl: "http://127.0.0.1:8102/v1", models: ["gpt-6.1-sol"] },
];
const KEY = "k".repeat(43);

describe("templates", () => {
  for (const name of TARGETS) {
    it(`${name} only uses known placeholders`, () => {
      loadTemplate(name); // validates fields
      const raw = readFileSync(path.join(sharedDir(), "templates", "config", `${name}.json`), "utf8");
      const used = new Set([...raw.matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]));
      const known = new Set(["provider", "PROVIDER", "provider_var", "display_name", "model", "base_url", "key"]);
      for (const placeholder of used) assert.ok(known.has(placeholder!), placeholder);
    });
  }

  it("litellm lists every model", () => {
    const text = render(loadTemplate("litellm"), TARGETS_LIST, null);
    assert.ok(text.startsWith("model_list:\n"));
    assert.equal(text.split("- model_name:").length - 1, 3);
    assert.ok(text.includes("model_name: claude-opus"));
    assert.ok(text.includes("model: openai/gpt-6.1-sol"));
    assert.ok(text.includes("api_base: http://127.0.0.1:8102/v1"));
    assert.equal(text.split("api_key: os.environ/MODELMUX_API_KEY").length - 1, 3);
  });

  for (const name of ["openai-python", "langchain"]) {
    it(`${name} refers to or quotes the key`, () => {
      assert.ok(render(loadTemplate(name), TARGETS_LIST, null).includes('os.environ["MODELMUX_API_KEY"]'));
      assert.ok(render(loadTemplate(name), TARGETS_LIST, KEY).includes(JSON.stringify(KEY)));
    });
  }

  it("langchain disables the Responses API", () => {
    assert.ok(render(loadTemplate("langchain"), TARGETS_LIST, null).includes("use_responses_api=False"));
  });

  it("per provider uses the first model", () => {
    const text = render(loadTemplate("openai-python"), TARGETS_LIST, null);
    assert.ok(text.includes('model="sonnet"'));
    assert.ok(!text.includes('model="opus"'));
  });

  it("curl and env", () => {
    const curl = render(loadTemplate("curl"), TARGETS_LIST, null);
    assert.ok(curl.includes("Authorization: Bearer $MODELMUX_API_KEY"));
    assert.equal(curl.split("curl ").length - 1, 2);
    const env = render(loadTemplate("env"), TARGETS_LIST, null);
    assert.deepEqual(env.trimEnd().split("\n"), [
      "MODELMUX_API_KEY=",
      "MODELMUX_CLAUDE_BASE_URL=http://127.0.0.1:8101/v1",
      "MODELMUX_CLAUDE_MODEL=sonnet",
      "MODELMUX_CODEX_BASE_URL=http://127.0.0.1:8102/v1",
      "MODELMUX_CODEX_MODEL=gpt-6.1-sol",
    ]);
    assert.ok(render(loadTemplate("env"), TARGETS_LIST, KEY).startsWith(`MODELMUX_API_KEY=${KEY}\n`));
  });

  it("rejects an unknown placeholder", () => {
    const bad = { ...loadTemplate("curl"), entry: "{{secret_sauce}}" };
    assert.throws(() => render(bad, TARGETS_LIST, null), (e: unknown) => e instanceof CliError && /Unknown placeholder/.test(e.message));
  });

  it("a missing template", () => {
    assert.throws(() => loadTemplate("nope"), (e: unknown) => e instanceof CliError && /missing or invalid/.test(e.message));
  });
});

describe("config and key commands", () => {
  const env = useEnv();

  async function readyHome(): Promise<string> {
    commandWorld(env);
    env.docker.when("run", { returns: [1, "", ""] }); // nobody logged in
    assert.equal((await cli("up")).code, 0);
    return env.home;
  }

  const storedKey = (home: string): string =>
    readFileSync(path.join(home, "secrets.env"), "utf8").trim().split("=")[1]!;

  function fakeModels(byPort: Record<number, string[]>): void {
    server.getJson = async (port) => {
      const models = byPort[port];
      if (!models) return [0, null];
      return [200, { object: "list", data: models.map((id) => ({ id })) }];
    };
  }

  it("key show", async () => {
    const home = await readyHome();
    const { code, out, err } = await cli("key", "show");
    assert.equal(code, 0);
    assert.equal(out, storedKey(home) + "\n");
    assert.match(err, /password/);
  });

  it("key show when not set up", async () => {
    commandWorld(env);
    const { code, err } = await cli("key", "show");
    assert.equal(code, 1);
    assert.match(err, /modelmux up/);
  });

  it("config uses live models and hides the key", async () => {
    const home = await readyHome();
    fakeModels({ 8101: ["sonnet", "opus"] });
    const { code, out, err } = await cli("config", "litellm");
    assert.equal(code, 0);
    assert.ok(out.includes("model_name: claude-opus"));
    assert.ok(out.includes("model: openai/gpt-6.1-sol")); // example model for codex
    assert.ok(!(out + err).includes(storedKey(home)));
    assert.match(err, /OpenAI Codex is not running/);
    assert.ok(err.includes("export MODELMUX_API_KEY=$(modelmux key show)"));
    assert.ok(!out.includes("not running")); // notes never pollute stdout
  });

  it("config --reveal-key", async () => {
    const home = await readyHome();
    fakeModels({});
    const { code, out } = await cli("config", "curl", "--provider", "claude", "--reveal-key");
    assert.equal(code, 0);
    assert.ok(out.includes(`Bearer ${storedKey(home)}`));
    assert.ok(!out.toLowerCase().includes("codex"));
  });

  it("config when not set up", async () => {
    commandWorld(env);
    const { code, err } = await cli("config", "env");
    assert.equal(code, 1);
    assert.match(err, /modelmux up/);
  });
});

