import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { beforeEach, describe, it } from "node:test";

import { Config, ConfigFileError, loadConfig, saveConfig } from "../src/config.ts";
import { tighten, writePrivate } from "../src/files.ts";
import { configDir, ensurePrivateDir } from "../src/paths.ts";
import { clearSecrets, MASK, redact } from "../src/redact.ts";
import { ensureApiKey, readApiKey, SecretsFileError } from "../src/secretsStore.ts";

const posix = { skip: process.platform === "win32" ? "POSIX permissions" : false };
const mode = (file: string): number => statSync(file).mode & 0o777;
const temp = (): string => mkdtempSync(path.join(tmpdir(), "modelmux-storage-"));

describe("paths", () => {
  const cases: Array<[string, Record<string, string>, string]> = [
    ["linux", {}, "/home/user/.config/modelmux"],
    ["linux", { XDG_CONFIG_HOME: "/xdg" }, "/xdg/modelmux"],
    ["linux", { XDG_CONFIG_HOME: "relative" }, "/home/user/.config/modelmux"],
    ["darwin", {}, "/home/user/Library/Application Support/modelmux"],
    ["win32", { APPDATA: "C:\\Users\\u\\AppData\\Roaming" }, "C:\\Users\\u\\AppData\\Roaming\\modelmux"],
    ["win32", {}, "C:\\Users\\u\\AppData\\Roaming\\modelmux"],
    ["linux", { MODELMUX_CLI_HOME: "/custom" }, "/custom"],
    ["linux", { MODELMUX_CLI_HOME: "~/mm" }, path.join("/home/user", "mm")],
  ];
  for (const [platform, env, expected] of cases) {
    it(`${platform} ${JSON.stringify(env)}`, () => {
      const home = platform === "win32" ? "C:\\Users\\u" : "/home/user";
      assert.equal(configDir(env, platform as NodeJS.Platform, home), expected);
    });
  }

  it("private dir", posix, () => {
    const dir = ensurePrivateDir(path.join(temp(), "a", "b"));
    assert.equal(mode(dir), 0o700);
  });
});

describe("files", () => {
  it("writes privately and atomically", posix, () => {
    const dir = temp();
    const target = path.join(dir, "f");
    writePrivate(target, "one");
    assert.equal(readFileSync(target, "utf8"), "one");
    assert.equal(mode(target), 0o600);
    writePrivate(target, "two");
    assert.equal(readFileSync(target, "utf8"), "two");
    assert.deepEqual(readdirSync(dir), ["f"]); // no temp files left
  });

  it("cleans up on failure", () => {
    const dir = temp();
    // The rename fails: the target is a directory that is not empty.
    mkdirSync(path.join(dir, "f", "inside"), { recursive: true });
    assert.throws(() => writePrivate(path.join(dir, "f"), "x"));
    assert.deepEqual(readdirSync(dir), ["f"]); // the temporary file is gone
  });

  it("tightens permissions", posix, () => {
    const file = path.join(temp(), "f");
    writeFileSync(file, "x");
    chmodSync(file, 0o644);
    assert.equal(tighten(file), true);
    assert.equal(mode(file), 0o600);
    assert.equal(tighten(file), false);
  });
});

describe("config", () => {
  it("defaults", () => {
    const config = loadConfig(temp());
    assert.deepEqual(config, new Config());
    assert.equal(config.port("claude"), 8101);
    assert.equal(config.port("codex"), 8102);
  });

  it("round trip", () => {
    const dir = temp();
    const file = saveConfig(dir, new Config(new Map([["codex", 9000]]), "ghcr.io/x/y:1"));
    assert.equal(
      readFileSync(file, "utf8"),
      '{\n  "version": 1,\n  "ports": {\n    "codex": 9000\n  },\n  "image": "ghcr.io/x/y:1"\n}\n',
    );
    const loaded = loadConfig(dir);
    assert.equal(loaded.port("codex"), 9000);
    assert.equal(loaded.port("claude"), 8101);
    assert.equal(loaded.image, "ghcr.io/x/y:1");
  });

  const damaged: Array<[string, string]> = [
    ["{not json", "not valid JSON"],
    ['{"version": 2}', "unknown format version"],
    ['{"version": 1, "ports": []}', "'ports' must be an object"],
    ['{"version": 1, "ports": {"nope": 1}}', "unknown provider"],
    ['{"version": 1, "ports": {"claude": 0}}', "invalid port"],
    ['{"version": 1, "ports": {"claude": true}}', "invalid port"],
    ['{"version": 1, "image": 5}', "'image' must be a string"],
  ];
  for (const [content, problem] of damaged) {
    it(`reports ${problem}: ${content}`, () => {
      const dir = temp();
      const file = path.join(dir, "config.json");
      writeFileSync(file, content);
      assert.throws(() => loadConfig(dir), (e: unknown) => {
        assert.ok(e instanceof ConfigFileError);
        assert.ok(e.message.includes(problem), e.message);
        assert.ok(e.hint!.includes(file));
        return true;
      });
      assert.equal(readFileSync(file, "utf8"), content); // never overwritten
    });
  }
});

describe("API key", () => {
  beforeEach(() => clearSecrets());

  it("is created once", () => {
    const dir = temp();
    const first = ensureApiKey(dir);
    assert.ok(first.created);
    assert.equal(first.value.length, 43);
    const second = ensureApiKey(dir);
    assert.ok(!second.created);
    assert.equal(second.value, first.value);
    assert.equal(readFileSync(first.path, "utf8"), `MODELMUX_API_KEYS=${first.value}\n`);
  });

  it("is random", () => {
    assert.notEqual(ensureApiKey(path.join(temp(), "a")).value, ensureApiKey(path.join(temp(), "b")).value);
  });

  it("is private", posix, () => {
    const dir = temp();
    const key = ensureApiKey(dir);
    assert.equal(mode(key.path), 0o600);
    chmodSync(key.path, 0o644);
    const again = readApiKey(dir);
    assert.ok(again?.permissionsFixed);
    assert.equal(mode(key.path), 0o600);
  });

  it("is redacted once loaded", () => {
    const dir = temp();
    const key = ensureApiKey(dir);
    assert.equal(redact(`value ${key.value}`), `value ${MASK}`);
    clearSecrets();
    readApiKey(dir);
    assert.ok(!redact(key.value).includes(key.value));
  });

  it("missing", () => {
    assert.equal(readApiKey(temp()), null);
  });

  for (const content of [
    "",
    "MODELMUX_API_KEYS=short\n",
    "OTHER=" + "a".repeat(40) + "\n",
    "MODELMUX_API_KEYS=" + "a".repeat(40) + "\nEXTRA=1\n",
    "MODELMUX_API_KEYS=has space " + "a".repeat(40),
  ]) {
    it(`damaged: ${JSON.stringify(content.slice(0, 24))}`, () => {
      const dir = temp();
      writeFileSync(path.join(dir, "secrets.env"), content);
      assert.throws(() => readApiKey(dir), (e: unknown) => e instanceof SecretsFileError && !e.message.includes("a".repeat(40)));
    });
  }

  it("reads a key written by the Python CLI", () => {
    const dir = temp();
    const value = "AbC_dEf-" + "x".repeat(35);
    writeFileSync(path.join(dir, "secrets.env"), `MODELMUX_API_KEYS=${value}\n`, { mode: 0o600 });
    assert.equal(readApiKey(dir)?.value, value);
  });
});
