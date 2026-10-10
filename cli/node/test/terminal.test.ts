import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, describe, it } from "node:test";

import { Console } from "../src/console.ts";
import { Docker } from "../src/docker.ts";
import { Timeout } from "../src/errors.ts";
import { loadProviders } from "../src/providers.ts";
import { LinkScanner, stripEscapes } from "../src/terminal.ts";
import { Capture, FIXTURES } from "./helpers.ts";

export const CLAUDE_URL =
  "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-" +
  "5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth" +
  "%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3A" +
  "sessions%3Aclaude_code+user%3Amcp_servers+user%3Afile_upload+user%3Aplugins&code_challenge" +
  "=FAKECHALLENGE00000000000000000000000000000&code_challenge_method=S256&state=" +
  "FAKESTATE0000000000000000000000000000000";

const fixture = (name: string): Buffer => readFileSync(path.join(FIXTURES, "login", name));

function feedInChunks(scanner: LinkScanner, data: Buffer, size: number): string[] {
  const found: string[] = [];
  for (let i = 0; i < data.length; i += size) {
    const link = scanner.feed(data.subarray(i, i + size));
    if (link !== null) found.push(link);
  }
  return found;
}

describe("link scanner", () => {
  for (const size of [1, 7, 64, 4096]) {
    it(`finds the Claude link in ${size}-byte chunks`, () => {
      const provider = loadProviders().get("claude")!;
      assert.deepEqual(feedInChunks(new LinkScanner(provider.linkPattern), fixture("claude_browser.raw"), size), [
        CLAUDE_URL,
      ]);
    });
  }

  for (const size of [1, 5, 4096]) {
    it(`finds the Codex link in ${size}-byte chunks`, () => {
      const provider = loadProviders().get("codex")!;
      assert.deepEqual(feedInChunks(new LinkScanner(provider.linkPattern), fixture("codex_device.raw"), size), [
        "https://auth.openai.com/codex/device",
      ]);
    });
  }

  it("strips escapes", () => {
    const text = stripEscapes(fixture("claude_browser.raw"));
    assert.ok(!text.includes("\x1b"));
    assert.ok(!text.includes("\x07"));
    assert.equal(text.split("https://").length - 1, 1); // the OSC 8 copy is gone
    assert.ok(text.includes("Paste code here if prompted >"));
    assert.ok(stripEscapes(fixture("codex_device.raw")).includes("ABCD-12345"));
  });

  it("does not report an incomplete link", () => {
    const scanner = new LinkScanner(/https:\/\/\S+/);
    assert.equal(scanner.feed(Buffer.from("visit https://example.com/long/pa")), null);
    assert.equal(scanner.feed(Buffer.from("th?x=1")), null);
    assert.equal(scanner.feed(Buffer.from("\r\n")), "https://example.com/long/path?x=1");
    assert.equal(scanner.feed(Buffer.from("https://other.example.com/ ")), null); // first link only
  });

  it("no link", () => {
    assert.equal(new LinkScanner(/https:\/\/\S+/).feed(Buffer.from("no links here\n")), null);
  });
});

// ------------------------------------------------------------------ the terminal relay

const FAKE_DOCKER = `#!${process.execPath}
const fs = require("node:fs");
process.stdout.write(fs.readFileSync(process.env.FAKE_FIXTURE));
if (process.env.FAKE_HANG) setTimeout(() => {}, 60000);
else process.exitCode = Number(process.env.FAKE_EXIT || "0");
`;

describe("terminal relay", { skip: process.platform === "win32" ? "the relay is POSIX-only" : false }, () => {
  const dir = mkdtempSync(path.join(tmpdir(), "modelmux-relay-"));
  const bin = path.join(dir, "docker");
  writeFileSync(bin, FAKE_DOCKER);
  chmodSync(bin, 0o700);
  after(() => rmSync(dir, { recursive: true, force: true }));

  const make = (): Docker =>
    new Docker(new Console({ out: new Capture(), err: new Capture() }), { which: () => bin });

  it("relays output, adds extra text and returns the exit code", async () => {
    process.env.FAKE_FIXTURE = path.join(FIXTURES, "login", "codex_device.raw");
    process.env.FAKE_EXIT = "3";
    const seen: Buffer[] = [];
    const shown: Buffer[] = [];
    const code = await make().runTty(["run", "x"], {
      onOutput: (data) => {
        seen.push(data);
        return seen.length === 1 ? Buffer.from("[EXTRA]") : null;
      },
      timeout: 10,
      stdout: { write: (data: Buffer) => shown.push(data) },
    });
    delete process.env.FAKE_EXIT;
    const text = Buffer.concat(shown).toString();
    assert.equal(code, 3);
    assert.ok(text.includes("auth.openai.com/codex/device"));
    assert.ok(text.includes("[EXTRA]"));
    assert.equal(text.replace("[EXTRA]", ""), Buffer.concat(seen).toString());
  });

  it("kills the program after the timeout", async () => {
    process.env.FAKE_FIXTURE = path.join(FIXTURES, "login", "codex_device.raw");
    process.env.FAKE_HANG = "1";
    const started = performance.now();
    await assert.rejects(
      make().runTty(["run"], { onOutput: () => null, timeout: 1, stdout: { write: () => true } }),
      Timeout,
    );
    delete process.env.FAKE_HANG;
    assert.ok(performance.now() - started < 5000);
  });
});
