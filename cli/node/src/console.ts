/**
 * Terminal output: short, friendly lines with ✓ / ✗ markers.
 *
 * Colour is used only on a real terminal, never when `NO_COLOR` is set or
 * `TERM=dumb`. Symbols fall back to ASCII when the terminal cannot show them
 * (older Windows consoles).
 */

import { redact } from "./redact.ts";

/** Where output goes: process.stdout / process.stderr, or a buffer in tests. */
export interface Stream {
  write(text: string): unknown;
  isTTY?: boolean;
}

const RESET = "\x1b[0m";
const STYLES = {
  ok: "\x1b[32m",
  error: "\x1b[31m",
  warn: "\x1b[33m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
} as const;
const SYMBOLS = {
  ok: ["✓", "OK"],
  error: ["✗", "X"],
  warn: ["!", "!"],
  step: ["→", ">"],
} as const;

type Style = keyof typeof STYLES;
type SymbolKind = keyof typeof SYMBOLS;

/**
 * Can this terminal show ✓✗→? Node always writes UTF-8; only legacy Windows
 * consoles (not Windows Terminal or VS Code) show it as garbage.
 */
function supportsUnicode(env: NodeJS.ProcessEnv = process.env): boolean {
  if (process.platform !== "win32") return true;
  return Boolean(
    env.WT_SESSION || env.TERM_PROGRAM === "vscode" || env.TERM === "xterm-256color" || env.CI,
  );
}

export interface ConsoleOptions {
  out?: Stream;
  err?: Stream;
  verbose?: boolean;
  color?: boolean | null;
  unicode?: boolean;
}

export class Console {
  readonly out: Stream;
  readonly err: Stream;
  readonly verbose: boolean;
  readonly color: boolean;
  readonly unicode: boolean;

  constructor(options: ConsoleOptions = {}) {
    this.out = options.out ?? process.stdout;
    this.err = options.err ?? process.stderr;
    this.verbose = options.verbose ?? false;
    let color = options.color ?? null;
    if (color === null) {
      // Node enables ANSI escape handling in the Windows console by itself.
      color =
        this.out.isTTY === true && !("NO_COLOR" in process.env) && process.env.TERM !== "dumb";
    }
    this.color = color;
    this.unicode = options.unicode ?? supportsUnicode();
  }

  private style(kind: Style, text: string): string {
    return this.color ? `${STYLES[kind]}${text}${RESET}` : text;
  }

  private symbol(kind: SymbolKind): string {
    const [fancy, plain] = SYMBOLS[kind];
    return this.unicode ? fancy : plain;
  }

  private line(stream: Stream, text: string): void {
    stream.write(redact(text) + "\n");
  }

  // ------------------------------------------------------------ public API

  /** Plain output (command results, config snippets). */
  print(text = ""): void {
    this.line(this.out, text);
  }

  success(text: string): void {
    this.line(this.out, `${this.style("ok", this.symbol("ok"))} ${text}`);
  }

  step(text: string): void {
    this.line(this.out, `${this.style("dim", this.symbol("step"))} ${text}`);
  }

  warn(text: string): void {
    this.line(this.err, `${this.style("warn", this.symbol("warn"))} ${text}`);
  }

  error(text: string, hint: string | null = null): void {
    this.line(this.err, `${this.style("error", this.symbol("error"))} ${text}`);
    if (hint) this.line(this.err, `  ${this.style("dim", hint)}`);
  }

  /** A hint on stderr, so stdout stays clean for piping (`config > file`). */
  note(text: string): void {
    this.line(this.err, `${this.style("dim", this.symbol("step"))} ${text}`);
  }

  /** Print WITHOUT redaction. Only for an explicit user request to see a secret. */
  reveal(text: string): void {
    this.out.write(text + "\n");
  }

  /** Only shown with --verbose. */
  detail(text: string): void {
    if (this.verbose) this.line(this.err, this.style("dim", `  ${text}`));
  }
}
