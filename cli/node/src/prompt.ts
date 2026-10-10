/**
 * Questions on the terminal: a yes/no answer (Python's `input`) and a hidden
 * secret (Python's `getpass`). Neither answer is stored or logged.
 */

import { createInterface } from "node:readline";

import { Interrupted } from "./errors.ts";

/** Read one line from stdin (no echo control). Rejects at end of input. */
function readLine(prompt: string, output: NodeJS.WritableStream): Promise<string> {
  return new Promise((resolve, reject) => {
    const rl = createInterface({ input: process.stdin, output, terminal: false });
    let answered = false;
    output.write(prompt);
    rl.once("line", (line) => {
      answered = true;
      rl.close();
      resolve(line);
    });
    rl.once("close", () => {
      if (!answered) reject(new Error("EOF when reading a line"));
    });
  });
}

/** Python's `input(question)`: the prompt goes to stdout. */
export function ask(question: string): Promise<string> {
  return readLine(question, process.stdout);
}

/** Python's `getpass.getpass(prompt)`: read a line without echoing it. */
export function readSecret(prompt: string): Promise<string> {
  const stdin = process.stdin;
  if (!stdin.isTTY) {
    process.stderr.write("Warning: Password input may be echoed.\n");
    return readLine(prompt, process.stderr);
  }
  return new Promise((resolve, reject) => {
    let value = "";
    process.stderr.write(prompt);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    const finish = (error: Error | null): void => {
      stdin.removeListener("data", onData);
      stdin.setRawMode(false);
      stdin.pause();
      process.stderr.write("\n");
      if (error) reject(error);
      else resolve(value);
    };
    const onData = (chunk: string): void => {
      for (const ch of chunk) {
        if (ch === "\r" || ch === "\n") return finish(null);
        if (ch === "\x03") return finish(new Interrupted()); // Ctrl+C
        if (ch === "\x04" && value === "") return finish(new Error("EOF when reading a line"));
        if (ch === "\x7f" || ch === "\b") value = value.slice(0, -1);
        else if (ch === "\x15") value = ""; // Ctrl+U
        else if (ch >= " ") value += ch;
      }
    };
    stdin.on("data", onData);
  });
}
