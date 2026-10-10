/** Small, safe file writes: atomic, and private (0600) from the first byte. */

import { randomBytes } from "node:crypto";
import { chmodSync, closeSync, fsyncSync, openSync, renameSync, statSync, unlinkSync, writeSync } from "node:fs";
import path from "node:path";

export const PRIVATE_MODE = 0o600;

/**
 * Atomically replace `file` with `data`, created with mode 0600.
 *
 * The temporary file is created with 0600 (never readable by others, even
 * briefly) in the same directory, then renamed over the target.
 */
export function writePrivate(file: string, data: string): void {
  const tmp = path.join(
    path.dirname(file),
    `.${path.basename(file)}.${randomBytes(6).toString("hex")}.tmp`,
  );
  const fd = openSync(tmp, "wx", PRIVATE_MODE);
  try {
    try {
      writeSync(fd, Buffer.from(data, "utf8"));
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(tmp, file);
  } catch (error) {
    try {
      unlinkSync(tmp);
    } catch {
      // already gone
    }
    throw error;
  }
}

/** Make an existing file 0600 on POSIX. Returns true if it had to change. */
export function tighten(file: string): boolean {
  if (process.platform === "win32") return false;
  const mode = statSync(file).mode & 0o777;
  if (mode !== PRIVATE_MODE) {
    chmodSync(file, PRIVATE_MODE);
    return true;
  }
  return false;
}
