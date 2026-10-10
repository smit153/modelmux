import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, it } from "node:test";

import { browserCommand } from "../src/browser.ts";
import { which } from "../src/docker.ts";

const URL = "https://example.com/login?x=1";

function binDir(...names: string[]): string {
  const dir = mkdtempSync(path.join(tmpdir(), "modelmux-bin-"));
  for (const name of names) {
    writeFileSync(path.join(dir, name), "#!/bin/sh\n");
    chmodSync(path.join(dir, name), 0o755);
  }
  return dir;
}

describe("browser", { skip: process.platform === "win32" ? "POSIX paths" : false }, () => {
  it("macOS uses open", () => {
    assert.deepEqual(browserCommand(URL, { PATH: "" }, "darwin"), ["open", URL]);
  });

  it("Linux needs a desktop session", () => {
    const dir = binDir("xdg-open");
    assert.equal(browserCommand(URL, { PATH: dir }, "linux"), null);
    assert.deepEqual(browserCommand(URL, { PATH: dir, DISPLAY: ":0" }, "linux"), ["xdg-open", URL]);
    assert.deepEqual(browserCommand(URL, { PATH: dir, WAYLAND_DISPLAY: "w" }, "linux"), ["xdg-open", URL]);
  });

  it("Linux without an opener", () => {
    assert.equal(browserCommand(URL, { PATH: binDir(), DISPLAY: ":0" }, "linux"), null);
  });

  it("$BROWSER wins, with or without %s", () => {
    const dir = binDir("wslview", "firefox");
    assert.deepEqual(browserCommand(URL, { PATH: dir, BROWSER: "wslview" }, "linux"), ["wslview", URL]);
    assert.deepEqual(browserCommand(URL, { PATH: dir, BROWSER: "missing:firefox --new-tab %s" }, "linux"), [
      "firefox", "--new-tab", URL,
    ]);
  });

  it("which finds executables only", () => {
    const dir = binDir("tool");
    writeFileSync(path.join(dir, "plain"), "x");
    assert.equal(which("tool", { PATH: dir }, "linux"), path.join(dir, "tool"));
    assert.equal(which("plain", { PATH: dir }, "linux"), null);
    assert.equal(which("nope", { PATH: dir }, "linux"), null);
  });
});
