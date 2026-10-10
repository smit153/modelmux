/** This package's version, from package.json (one level above src/ and dist/). */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

function readVersion(): string {
  try {
    const file = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "package.json");
    const data = JSON.parse(readFileSync(file, "utf8")) as { version?: unknown };
    return typeof data.version === "string" ? data.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}

export const VERSION = readVersion();
