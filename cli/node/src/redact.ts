/** Mask secrets in anything the CLI prints (including --verbose output). */

export const MASK = "[REDACTED]";

const PATTERNS: ReadonlyArray<readonly [RegExp, string]> = [
  [/\b(bearer)\s+[A-Za-z0-9._~+/=-]+/gi, `$1 ${MASK}`],
  [/\bsk-[A-Za-z0-9_-]{8,}/g, MASK],
  [/\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*/g, MASK],
  // The server's key list: only values that look like keys, not "$(...)" hints.
  [/\b(MODELMUX_API_KEYS=)[A-Za-z0-9_,-]{16,}/g, `$1${MASK}`],
];

const secrets = new Set<string>();

/** Mask these exact values everywhere from now on (e.g. the server API key). */
export function registerSecret(...values: string[]): void {
  for (const value of values) {
    if (value.length >= 8) secrets.add(value);
  }
}

export function registered(): ReadonlySet<string> {
  return new Set(secrets);
}

/** Forget every registered secret (tests). */
export function clearSecrets(): void {
  secrets.clear();
}

export function redact(text: string): string {
  for (const secret of [...secrets].sort((a, b) => b.length - a.length)) {
    text = text.split(secret).join(MASK);
  }
  for (const [pattern, replacement] of PATTERNS) {
    text = text.replace(pattern, replacement);
  }
  return text;
}
