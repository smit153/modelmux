import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

import { VERSION } from "../src/version.ts";
import { release, releaseData, resetReleaseCache } from "../src/release.ts";

const REPO = path.resolve(import.meta.dirname, "..", "..", "..");

function pyprojectVersion(file: string): string {
  const match = /^version = "([^"]+)"$/m.exec(readFileSync(file, "utf8"));
  assert.ok(match, file);
  return match[1]!;
}

describe("release", () => {
  it("one version for everything: both CLIs, the server and the release data", () => {
    const node = JSON.parse(readFileSync(path.join(REPO, "cli", "node", "package.json"), "utf8")).version;
    const python = pyprojectVersion(path.join(REPO, "cli", "python", "pyproject.toml"));
    const server = pyprojectVersion(path.join(REPO, "server", "pyproject.toml"));
    const data = JSON.parse(readFileSync(path.join(REPO, "shared", "release.json"), "utf8")).version;
    assert.equal(node, python);
    assert.equal(node, server);
    assert.equal(node, data);
    assert.equal(VERSION, node);
  });

  it("a development checkout has no pinned image", () => {
    resetReleaseCache();
    assert.equal(release.pinnedImage(), null);
    assert.equal(releaseData().version, VERSION);
  });
});
