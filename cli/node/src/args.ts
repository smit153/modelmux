/**
 * A small command-line parser that behaves like Python's argparse, so the
 * Node CLI accepts exactly the same command lines as the Python one and
 * answers with the same help, usage and error text (exit code 2).
 *
 * Supported: subcommands, store_true / store / append options, --version,
 * -h/--help, choices, int values, positionals with nargs 1, "?" and "*",
 * unique prefixes of long options (--verb -> --verbose), --opt=value,
 * clusters of short flags (-fv), "--", and negative numbers as values.
 */

import { repr } from "./text.ts";

export type Namespace = Record<string, unknown>;

export interface OptionSpec {
  flags: string[];
  dest?: string;
  action?: "store" | "store_true" | "append" | "version" | "help";
  metavar?: string;
  choices?: readonly string[];
  type?: "int";
  default?: unknown;
  help?: string;
  version?: string;
}

export interface PositionalSpec {
  dest: string;
  nargs?: "?" | "*";
  metavar?: string;
  choices?: readonly string[];
  help?: string;
}

/** Raised for `-h` and `--version`: print `text` to stdout and exit 0. */
export class ParserExit extends Error {
  readonly status: number;
  readonly text: string;

  constructor(status: number, text: string) {
    super(text);
    this.status = status;
    this.text = text;
  }
}

/** A usage error: `text` (usage + "prog: error: ...") goes to stderr, exit 2. */
export class ParserError extends Error {
  readonly status = 2;
  readonly text: string;

  constructor(text: string) {
    super(text);
    this.text = text;
  }
}

/** Python's `shutil.get_terminal_size().columns`. */
function terminalColumns(): number {
  const env = Number.parseInt(process.env.COLUMNS ?? "", 10);
  if (env > 0) return env;
  return process.stdout.columns && process.stdout.columns > 0 ? process.stdout.columns : 80;
}

/** Python's `textwrap.wrap` after argparse collapses whitespace. */
function wrap(text: string, width: number): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

const INT = /^\s*[+-]?\d+(_\d+)*\s*$/;
const NEGATIVE_NUMBER = /^-\d+$|^-\d*\.\d+$/;

interface Option extends OptionSpec {
  dest: string;
  action: NonNullable<OptionSpec["action"]>;
}

interface Subcommand {
  name: string;
  help: string;
  parser: Parser;
}

export class Parser {
  readonly prog: string;
  readonly description: string | null;
  readonly epilog: string | null;
  private readonly options: Option[] = [];
  private readonly positionals: PositionalSpec[] = [];
  private sub: { dest: string; metavar: string; commands: Subcommand[] } | null = null;
  private readonly defaults: Namespace = {};

  constructor(prog: string, options: { description?: string; epilog?: string } = {}) {
    this.prog = prog;
    this.description = options.description ?? null;
    this.epilog = options.epilog ?? null;
    this.addOption({ flags: ["-h", "--help"], action: "help", help: "show this help message and exit" });
  }

  addOption(spec: OptionSpec): this {
    const long = spec.flags.find((f) => f.startsWith("--")) ?? spec.flags[0]!;
    const dest = spec.dest ?? long.replace(/^-+/, "").replaceAll("-", "_");
    const action = spec.action ?? "store";
    this.options.push({ ...spec, dest, action });
    if (action === "store_true") this.defaults[dest] = spec.default ?? false;
    else if (action === "store" || action === "append") this.defaults[dest] = spec.default ?? null;
    return this;
  }

  addPositional(spec: PositionalSpec): this {
    this.positionals.push(spec);
    if (spec.nargs === "*") this.defaults[spec.dest] = [];
    else if (spec.nargs === "?") this.defaults[spec.dest] = null;
    return this;
  }

  addSubcommands(dest: string, metavar: string): this {
    this.sub = { dest, metavar, commands: [] };
    return this;
  }

  addCommand(name: string, help: string, configure: (parser: Parser) => void = () => undefined): Parser {
    if (this.sub === null) throw new Error("no subcommands");
    const parser = new Parser(`${this.prog} ${name}`);
    configure(parser);
    this.sub.commands.push({ name, help, parser });
    return parser;
  }

  setDefaults(values: Namespace): this {
    Object.assign(this.defaults, values);
    return this;
  }

  // ---------------------------------------------------------------- parsing

  /** Parse `argv`. Throws `ParserExit` (help/version) or `ParserError`. */
  parse(argv: readonly string[]): Namespace {
    const namespace: Namespace = {};
    const extras = this.parseInto(argv, namespace);
    if (extras.length) this.error(`unrecognized arguments: ${extras.join(" ")}`);
    return namespace;
  }

  private looksLikeOption(arg: string): boolean {
    return arg.startsWith("-") && arg !== "-" && !NEGATIVE_NUMBER.test(arg);
  }

  /** Find the option an argument names: exact, `--opt=value`, or a unique long prefix. */
  private match(arg: string): { option: Option; inline: string | null } | null {
    const [name, inline] = arg.includes("=") && arg.startsWith("--")
      ? [arg.slice(0, arg.indexOf("=")), arg.slice(arg.indexOf("=") + 1)]
      : [arg, null];
    const exact = this.options.find((o) => o.flags.includes(name));
    if (exact) return { option: exact, inline };
    if (name.startsWith("--")) {
      const hits: Array<[Option, string]> = [];
      for (const option of this.options) {
        for (const flag of option.flags) {
          if (flag.startsWith("--") && flag.startsWith(name)) hits.push([option, flag]);
        }
      }
      if (hits.length > 1) {
        const names = hits.map(([, flag]) => flag).join(", ");
        this.error(`ambiguous option: ${name} could match ${names}`);
      }
      if (hits.length === 1) return { option: hits[0]![0], inline };
    }
    return null;
  }

  private convert(option: Option, value: string): unknown {
    if (option.type === "int") {
      if (!INT.test(value)) this.error(`argument ${option.flags.join("/")}: invalid int value: ${repr(value)}`);
      return Number.parseInt(value.replaceAll("_", "").trim(), 10);
    }
    if (option.choices && !option.choices.includes(value)) {
      this.error(
        `argument ${option.flags.join("/")}: invalid choice: ${repr(value)} ` +
          `(choose from ${option.choices.map(repr).join(", ")})`,
      );
    }
    return value;
  }

  private apply(option: Option, value: string | null, namespace: Namespace): void {
    switch (option.action) {
      case "help":
        throw new ParserExit(0, this.formatHelp());
      case "version":
        throw new ParserExit(0, `${option.version ?? ""}\n`);
      case "store_true":
        namespace[option.dest] = true;
        return;
      case "store":
        namespace[option.dest] = this.convert(option, value!);
        return;
      case "append": {
        const list = Array.isArray(namespace[option.dest]) ? (namespace[option.dest] as unknown[]) : [];
        namespace[option.dest] = [...list, this.convert(option, value!)];
        return;
      }
    }
  }

  private positionalName(spec: PositionalSpec): string {
    return spec.metavar ?? spec.dest;
  }

  private checkChoice(spec: PositionalSpec, value: string): string {
    if (spec.choices && !spec.choices.includes(value)) {
      this.error(
        `argument ${this.positionalName(spec)}: invalid choice: ${repr(value)} ` +
          `(choose from ${spec.choices.map(repr).join(", ")})`,
      );
    }
    return value;
  }

  /** Parse into `namespace`; returns arguments nobody recognised. */
  private parseInto(argv: readonly string[], namespace: Namespace): string[] {
    for (const [key, value] of Object.entries(this.defaults)) {
      if (!(key in namespace)) namespace[key] = Array.isArray(value) ? [...value] : value;
    }
    const extras: string[] = [];
    const values: string[] = [];
    let i = 0;
    let onlyPositionals = false;
    let subcommandAt = -1;
    while (i < argv.length) {
      const arg = argv[i]!;
      if (!onlyPositionals && arg === "--") {
        onlyPositionals = true;
        i++;
        continue;
      }
      if (onlyPositionals || !this.looksLikeOption(arg)) {
        if (this.sub !== null && values.length === this.positionals.length) {
          subcommandAt = i;
          break;
        }
        values.push(arg);
        i++;
        continue;
      }
      const found = this.match(arg);
      if (found !== null) {
        const { option, inline } = found;
        if (option.action === "store" || option.action === "append") {
          let value = inline;
          if (value === null) {
            const next = argv[i + 1];
            if (next === undefined || this.looksLikeOption(next)) {
              this.error(`argument ${option.flags.join("/")}: expected one argument`);
            }
            value = next;
            i++;
          }
          this.apply(option, value, namespace);
        } else {
          if (inline !== null) {
            this.error(`argument ${option.flags.join("/")}: ignored explicit argument ${repr(inline)}`);
          }
          this.apply(option, null, namespace);
        }
        i++;
        continue;
      }
      // A cluster of short flags, e.g. -fv.
      if (/^-[A-Za-z]{2,}$/.test(arg)) {
        const flags = [...arg.slice(1)].map((ch) => this.options.find((o) => o.flags.includes(`-${ch}`)));
        if (flags.every((o) => o !== undefined && o.action !== "store" && o.action !== "append")) {
          for (const option of flags) this.apply(option!, null, namespace);
          i++;
          continue;
        }
      }
      extras.push(arg);
      i++;
    }
    this.assignPositionals(values, namespace, extras);
    if (this.sub !== null) {
      if (subcommandAt < 0) {
        this.error(`the following arguments are required: ${this.sub.metavar}`);
      }
      const name = argv[subcommandAt]!;
      const command = this.sub.commands.find((c) => c.name === name);
      if (command === undefined) {
        this.error(
          `argument ${this.sub.metavar}: invalid choice: ${repr(name)} ` +
            `(choose from ${this.sub.commands.map((c) => repr(c.name)).join(", ")})`,
        );
      }
      namespace[this.sub.dest] = name;
      extras.push(...command.parser.parseInto(argv.slice(subcommandAt + 1), namespace));
    }
    return extras;
  }

  private assignPositionals(values: string[], namespace: Namespace, extras: string[]): void {
    const queue = [...values];
    const missing: string[] = [];
    for (const spec of this.positionals) {
      if (spec.nargs === "*") {
        namespace[spec.dest] = queue.splice(0).map((v) => this.checkChoice(spec, v));
      } else if (spec.nargs === "?") {
        const value = queue.shift();
        namespace[spec.dest] = value === undefined ? null : this.checkChoice(spec, value);
      } else {
        const value = queue.shift();
        if (value === undefined) missing.push(this.positionalName(spec));
        else namespace[spec.dest] = this.checkChoice(spec, value);
      }
    }
    if (missing.length) this.error(`the following arguments are required: ${missing.join(", ")}`);
    extras.push(...queue);
  }

  error(message: string): never {
    throw new ParserError(`${this.formatUsage()}${this.prog}: error: ${message}\n`);
  }

  // ---------------------------------------------------------------- help

  private width(): number {
    return terminalColumns() - 2;
  }

  private optionUsage(option: Option): string {
    const flag = option.flags[0]!;
    if (option.action === "store" || option.action === "append") {
      return `[${flag} ${this.metavar(option)}]`;
    }
    return `[${flag}]`;
  }

  private metavar(option: Option): string {
    if (option.metavar) return option.metavar;
    if (option.choices) return `{${option.choices.join(",")}}`;
    return option.dest.toUpperCase();
  }

  private positionalUsage(spec: PositionalSpec): string {
    const name = spec.metavar ?? (spec.choices ? `{${spec.choices.join(",")}}` : spec.dest);
    if (spec.nargs === "?") return `[${name}]`;
    if (spec.nargs === "*") return `[${name} ...]`;
    return name;
  }

  private positionalInvocation(spec: PositionalSpec): string {
    return spec.metavar ?? (spec.choices ? `{${spec.choices.join(",")}}` : spec.dest);
  }

  formatUsage(): string {
    const prefix = "usage: ";
    const optParts = this.options.map((o) => this.optionUsage(o));
    const posParts = this.positionals.map((p) => this.positionalUsage(p));
    if (this.sub !== null) posParts.push(`${this.sub.metavar} ...`);
    const textWidth = this.width();
    const full = [this.prog, ...optParts, ...posParts].join(" ");
    if (prefix.length + full.length <= textWidth) return `${prefix}${full}\n`;

    const getLines = (parts: string[], indent: string, first: string | null = null): string[] => {
      const lines: string[] = [];
      let line: string[] = [];
      let length = first !== null ? first.length - 1 : indent.length - 1;
      for (const part of parts) {
        if (length + 1 + part.length > textWidth && line.length) {
          lines.push(indent + line.join(" "));
          line = [];
          length = indent.length - 1;
        }
        line.push(part);
        length += part.length + 1;
      }
      if (line.length) lines.push(indent + line.join(" "));
      if (first !== null && lines.length) lines[0] = lines[0]!.slice(indent.length);
      return lines;
    };

    let lines: string[];
    if (prefix.length + this.prog.length <= 0.75 * textWidth) {
      const indent = " ".repeat(prefix.length + this.prog.length + 1);
      if (optParts.length) {
        lines = getLines([this.prog, ...optParts], indent, prefix);
        lines.push(...getLines(posParts, indent));
      } else if (posParts.length) {
        lines = getLines([this.prog, ...posParts], indent, prefix);
      } else {
        lines = [this.prog];
      }
    } else {
      const indent = " ".repeat(prefix.length);
      lines = [this.prog, ...getLines([...optParts, ...posParts], indent)];
    }
    return `${prefix}${lines.join("\n")}\n`;
  }

  formatHelp(): string {
    const width = this.width();
    const maxHelpPosition = Math.min(24, Math.max(width - 20, 4));

    type Row = { invocation: string; help: string | undefined; indent: number };
    const positionalRows: Row[] = this.positionals.map((p) => ({
      invocation: this.positionalInvocation(p),
      help: p.help,
      indent: 2,
    }));
    if (this.sub !== null) {
      positionalRows.push({ invocation: this.sub.metavar, help: undefined, indent: 2 });
      for (const command of this.sub.commands) {
        positionalRows.push({ invocation: command.name, help: command.help, indent: 4 });
      }
    }
    const optionRows: Row[] = this.options.map((o) => ({
      invocation:
        o.action === "store" || o.action === "append"
          ? o.flags.map((f) => `${f} ${this.metavar(o)}`).join(", ")
          : o.flags.join(", "),
      help: o.help,
      indent: 2,
    }));

    const all = [...positionalRows, ...optionRows];
    const maxLength = Math.max(...all.map((r) => r.invocation.length + r.indent));
    const helpPosition = Math.min(maxLength + 2, maxHelpPosition);
    const helpWidth = Math.max(width - helpPosition, 11);

    const formatRow = (row: Row): string => {
      const actionWidth = helpPosition - row.indent - 2;
      const head = " ".repeat(row.indent) + row.invocation;
      if (!row.help) return `${head}\n`;
      const helpLines = wrap(row.help, helpWidth);
      let out: string;
      if (row.invocation.length <= actionWidth) {
        out = head.padEnd(helpPosition) + helpLines[0] + "\n";
      } else {
        out = `${head}\n${" ".repeat(helpPosition)}${helpLines[0]}\n`;
      }
      for (const line of helpLines.slice(1)) out += `${" ".repeat(helpPosition)}${line}\n`;
      return out;
    };

    const sections: string[] = [this.formatUsage()];
    if (this.description) sections.push(wrap(this.description, width).join("\n") + "\n");
    if (positionalRows.length) {
      sections.push("positional arguments:\n" + positionalRows.map(formatRow).join(""));
    }
    sections.push("options:\n" + optionRows.map(formatRow).join(""));
    if (this.epilog) sections.push(wrap(this.epilog, width).join("\n") + "\n");
    return sections.join("\n");
  }
}
