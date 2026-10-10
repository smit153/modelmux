/**
 * Find the login link in a provider CLI's terminal output.
 *
 * Output arrives in arbitrary chunks and contains escape sequences: colours
 * (CSI) and OSC 8 hyperlinks, which carry the URL a second time. Escapes are
 * removed from a rolling buffer, and a link is only reported once it is
 * complete, i.e. followed by whitespace, so a URL split across reads is never
 * opened half-finished.
 */

const OSC = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
const CSI = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
const OTHER_ESC = /\x1b[@-Z\\-_]/g;
const BUFFER_LIMIT = 64 * 1024;
// Python's str.isspace() for the characters a terminal can produce.
const SPACE = /^[\s\x1c-\x1f\x85]$/u;

/** Remove escape sequences (bytes are handled as latin1 so offsets stay byte offsets). */
export function stripEscapes(data: Buffer): string {
  let text = data.toString("latin1");
  text = text.replace(OSC, "").replace(CSI, "").replace(OTHER_ESC, "");
  return new TextDecoder("utf-8").decode(Buffer.from(text, "latin1"));
}

export class LinkScanner {
  readonly pattern: RegExp;
  private raw: Buffer = Buffer.alloc(0);
  link: string | null = null;

  constructor(pattern: RegExp) {
    this.pattern = new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : pattern.flags + "g");
  }

  /** Add output. Returns the link the first time a complete one is seen. */
  feed(data: Buffer): string | null {
    if (this.link !== null) return null;
    const joined = Buffer.concat([this.raw, data]);
    this.raw = joined.subarray(Math.max(0, joined.length - BUFFER_LIMIT));
    const text = stripEscapes(this.raw);
    this.pattern.lastIndex = 0;
    for (const match of text.matchAll(this.pattern)) {
      const end = match.index + match[0].length;
      if (end < text.length && SPACE.test(text[end]!)) {
        this.link = match[0];
        return this.link;
      }
    }
    return null;
  }
}
