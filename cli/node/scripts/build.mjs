// Build the package: compile src/ to dist/ and bundle the repository's
// language-neutral /shared data as dist/_shared (like hatch_build.py does for
// the Python CLI). Run by `npm run build` and automatically by `npm pack` /
// `npm publish` (prepack).

import { spawnSync } from "node:child_process";
import { chmodSync, cpSync, existsSync, rmSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");
const PARTS = ["providers", "schema", "templates"];
const FILES = ["release.json"];

const shared = join(root, "..", "..", "shared");
const complete =
  PARTS.every((p) => existsSync(join(shared, p)) && statSync(join(shared, p)).isDirectory()) &&
  FILES.every((f) => existsSync(join(shared, f)));
if (!complete) {
  console.error("modelmux shared data (providers, schema) not found");
  process.exit(1);
}

rmSync(dist, { recursive: true, force: true });
const tsc = createRequire(import.meta.url).resolve("typescript/bin/tsc");
const result = spawnSync(process.execPath, [tsc, "-p", join(root, "tsconfig.build.json")], {
  stdio: "inherit",
});
if (result.status !== 0) process.exit(result.status ?? 1);

for (const part of [...PARTS, ...FILES]) {
  cpSync(join(shared, part), join(dist, "_shared", part), { recursive: true });
}
if (process.platform !== "win32") chmodSync(join(dist, "main.js"), 0o755);
