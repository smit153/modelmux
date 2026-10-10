/** Python-compatible formatting, so messages match the Python CLI exactly. */

/** Python's `repr()` of a string: 'value' (or "value" if it contains only '). */
export function repr(value: string): string {
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = "";
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0;
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += `\\${ch}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (code < 0x20 || code === 0x7f) out += `\\x${code.toString(16).padStart(2, "0")}`;
    else out += ch;
  }
  return quote + out + quote;
}

/** Python's `shlex.quote()`. */
export function shellQuote(value: string): string {
  if (value === "") return "''";
  if (/^[\w@%+=:,./-]+$/.test(value)) return value;
  return "'" + value.replaceAll("'", `'"'"'`) + "'";
}

/** Python's `shlex.join()`. */
export function shellJoin(args: readonly string[]): string {
  return args.map(shellQuote).join(" ");
}

/** Sorted copy of string keys, like Python's `sorted()` on str (code point order). */
export function sortedStrings(values: Iterable<string>): string[] {
  return [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}
