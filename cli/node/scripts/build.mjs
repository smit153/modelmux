// Build steps around `tsc` (see "build" in package.json):
//   node scripts/build.mjs clean   remove dist/
//   node scripts/build.mjs shared  bundle the repository's language-neutral
//                                  /shared data as dist/_shared (like
//                                  hatch_build.py does for the Python CLI),
//                                  and the repository's LICENSE
// `npm pack` / `npm publish` run the whole build first (prepack).

import { chmodSync, cpSync, existsSync, rmSync, statSync } from "node:fs";
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

const step = process.argv[2];
if (step === "clean") {
  rmSync(dist, { recursive: true, force: true });
  process.exit(0);
}
if (step !== "shared") {
  console.error("usage: node scripts/build.mjs clean|shared");
  process.exit(2);
}

for (const part of [...PARTS, ...FILES]) {
  cpSync(join(shared, part), join(dist, "_shared", part), { recursive: true });
}
cpSync(join(root, "..", "..", "LICENSE"), join(root, "LICENSE"));
if (process.platform !== "win32") chmodSync(join(dist, "bin.js"), 0o755);
