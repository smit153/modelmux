/**
 * The release this CLI belongs to, from `shared/release.json`.
 *
 * The release workflow writes the server image pinned by digest
 * (`ghcr.io/smit153/modelmux@sha256:...`) into that file before building, so
 * every CLI implementation (Python and Node) runs the same image. In a
 * development checkout `image` is null: pass `--image` instead.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { CliError } from "./errors.ts";
import { sharedDir } from "./providers.ts";

export interface ReleaseData {
  readonly version: string;
  readonly image: string | null;
}

let cached: ReleaseData | null = null;

export function releaseData(): ReleaseData {
  if (cached !== null) return cached;
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(path.join(sharedDir(), "release.json"), "utf8"));
  } catch {
    throw new CliError("The release data is missing or damaged.", {
      hint: "Please report this bug.",
    });
  }
  if (
    typeof data !== "object" ||
    data === null ||
    Array.isArray(data) ||
    typeof (data as Record<string, unknown>).version !== "string"
  ) {
    throw new CliError("The release data is invalid.", { hint: "Please report this bug." });
  }
  const record = data as Record<string, unknown>;
  const image = record.image ?? null;
  if (image !== null && !(typeof image === "string" && image.includes("@sha256:"))) {
    throw new CliError("The pinned server image is not pinned by digest.", {
      hint: "Please report this bug.",
    });
  }
  cached = { version: record.version as string, image: typeof image === "string" ? image : null };
  return cached;
}

/** Forget the cached release data (tests). */
export function resetReleaseCache(): void {
  cached = null;
}

function readPinnedImage(): string | null {
  return releaseData().image;
}

/** Replaceable in tests. */
export const release = {
  pinnedImage: readPinnedImage,
};
