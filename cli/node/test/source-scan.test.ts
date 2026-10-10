/** No shells anywhere; only docker.ts (and browser.ts, for the system opener) may start processes. */

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

const SRC = path.resolve(import.meta.dirname, "..", "src");
const RUNNERS = new Set(["docker.ts", "browser.ts"]);
const PROCESS_MODULES = /["'](node:)?(child_process|cluster|worker_threads)["']/;
const SPAWN_CALLS = /\b(exec|execSync|execFile|execFileSync|spawnSync|fork)\s*\(/;

export function violations(file: string, source: string): string[] {
  const found: string[] = [];
  source.split("\n").forEach((line, i) => {
    const where = `${i + 1}`;
    // The spawn option (`shell: true`), not a key that holds a list or a tuple.
    if (/\bshell\s*:(?!\s*\[)/.test(line)) found.push(`${where}: shell option`);
    if (/\bprocess\.binding\s*\(/.test(line)) found.push(`${where}: process.binding`);
    if (!RUNNERS.has(path.basename(file))) {
      if (PROCESS_MODULES.test(line)) found.push(`${where}: imports a process module`);
    } else if (SPAWN_CALLS.test(line)) {
      found.push(`${where}: only spawn() with an argument list is allowed`);
    }
  });
  return found;
}

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".ts"))
    .map((entry) => path.join(entry.parentPath, entry.name));
}

describe("source scan", () => {
  for (const file of sources(SRC)) {
    it(path.relative(SRC, file), () => {
      assert.deepEqual(violations(file, readFileSync(file, "utf8")), []);
    });
  }

  it("catches violations", () => {
    const bad = [
      'import { spawn } from "node:child_process";',
      'spawn("x", [], { shell: true });',
      'import { Worker } from "worker_threads";',
    ].join("\n");
    assert.equal(violations("/x/bad.ts", bad).length, 3);
    assert.equal(violations("/x/docker.ts", 'execSync("ls");').length, 1);
  });
});
