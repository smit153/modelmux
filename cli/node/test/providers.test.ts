import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, it } from "node:test";

import { CliError, UsageError } from "../src/errors.ts";
import { getProvider, loadProviders, sharedDir } from "../src/providers.ts";

const SHARED = path.resolve(import.meta.dirname, "..", "..", "..", "shared");

describe("providers", () => {
  it("reads /shared from the repository", () => {
    assert.equal(sharedDir(), SHARED);
  });

  it("built-in providers", () => {
    const providers = loadProviders();
    assert.deepEqual([...providers.keys()], ["claude", "codex"]);
    const claude = providers.get("claude")!;
    assert.equal(claude.driver, "claude");
    assert.equal(claude.defaultPort, 8101);
    assert.equal(claude.volume, "claude-home");
    assert.equal(claude.home, "/home/modelmux/driver-home");
    assert.equal(claude.defaultLoginMethod, "browser");
    assert.deepEqual(claude.loginMethods.get("browser")!.command, ["claude", "auth", "login"]);
    assert.deepEqual(claude.statusCommand, ["claude", "auth", "status"]);
    assert.ok(new RegExp(claude.linkPattern.source).test("open https://example.com/x?y=1 now"));
    const codex = providers.get("codex")!;
    assert.equal(codex.defaultPort, 8102);
    assert.equal(codex.loginMethods.get("api-key")!.secretStdin, "OpenAI API key");
    const all = [...providers.values()];
    assert.equal(new Set(all.map((p) => p.volume)).size, all.length);
    assert.equal(new Set(all.map((p) => p.defaultPort)).size, all.length);
  });

  it("getProvider", () => {
    assert.equal(getProvider("codex").name, "codex");
    assert.throws(() => getProvider("nope"), (e: unknown) => {
      assert.ok(e instanceof UsageError);
      assert.match(e.message, /Unknown provider 'nope'/);
      assert.equal(e.hint, "Choose one of: claude, codex.");
      return true;
    });
  });
});

describe("invalid provider definitions", () => {
  const valid = (): Record<string, any> =>
    JSON.parse(readFileSync(path.join(SHARED, "providers", "claude.json"), "utf8"));

  function write(data: unknown, name = "claude", dir?: string): string {
    const directory = dir ?? path.join(mkdtempSync(path.join(tmpdir(), "modelmux-providers-")), "providers");
    mkdirSync(directory, { recursive: true });
    writeFileSync(path.join(directory, `${name}.json`), typeof data === "string" ? data : JSON.stringify(data));
    return directory;
  }

  const mutations: Record<string, (d: Record<string, any>) => void> = {
    schema: (d) => (d.schema_version = 2),
    name: (d) => (d.name = "other"),
    volume: (d) => (d.volume = "Bad Volume!"),
    home: (d) => (d.home = "relative"),
    port: (d) => (d.default_port = 80),
    "port-bool": (d) => (d.default_port = true),
    "port-float": (d) => (d.default_port = 8101.5),
    shell: (d) => (d.login.methods.browser.command = ["sh", "-c", "rm -rf /"]),
    "empty-command": (d) => (d.status.command = []),
    "default-method": (d) => (d.login.default_method = "missing"),
    regex: (d) => (d.login.link_pattern = "("),
    missing: (d) => delete d.display_name,
  };
  for (const [change, mutate] of Object.entries(mutations)) {
    it(`rejects ${change}`, () => {
      const data = valid();
      mutate(data);
      assert.throws(() => loadProviders(write(data)), (e: unknown) => e instanceof CliError && /claude\.json is invalid/.test(e.message));
    });
  }

  it("unreadable file", () => {
    assert.throws(() => loadProviders(write("{not json")), (e: unknown) => e instanceof CliError && /cannot be read \(JSONDecodeError\)/.test(e.message));
  });

  it("no providers", () => {
    const empty = mkdtempSync(path.join(tmpdir(), "modelmux-empty-"));
    assert.throws(() => loadProviders(empty), (e: unknown) => e instanceof CliError && /No provider definitions/.test(e.message));
  });

  it("shared volume", () => {
    const other = valid();
    other.name = "other";
    const directory = write(valid());
    write(other, "other", directory);
    assert.throws(() => loadProviders(directory), (e: unknown) => e instanceof CliError && /share a login volume/.test(e.message));
  });
});
