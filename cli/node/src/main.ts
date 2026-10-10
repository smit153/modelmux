/**
 * `modelmux`: set up, log in to and run ModelMux.
 *
 * This module only parses arguments and reports errors. Each command lives in
 * `commands/`.
 */

import { ParserError, ParserExit } from "./args.ts";
import { type Args, asArgs, buildParser } from "./cli.ts";
import * as basic from "./commands/basic.ts";
import * as configCmd from "./commands/configCmd.ts";
import * as doctor from "./commands/doctor.ts";
import * as login from "./commands/login.ts";
import * as logout from "./commands/logout.ts";
import * as up from "./commands/up.ts";
import * as upgrade from "./commands/upgrade.ts";
import { Console, type Stream } from "./console.ts";
import { CliError, EXIT_FAILURE, EXIT_INTERRUPTED, Interrupted } from "./errors.ts";
import { interrupt } from "./interrupt.ts";

export type Handler = (args: Args, console: Console) => Promise<number>;

const HANDLERS: Record<string, Handler> = {
  up: up.run,
  down: basic.down,
  logs: basic.logs,
  status: basic.status,
  login: login.run,
  logout: logout.run,
  config: configCmd.config,
  doctor: doctor.run,
  upgrade: upgrade.run,
  "key show": configCmd.keyShow,
};

export interface MainStreams {
  out?: Stream;
  err?: Stream;
}

export async function main(argv: readonly string[] = process.argv.slice(2), streams: MainStreams = {}): Promise<number> {
  const raw = [...argv];
  const out = streams.out ?? process.stdout;
  const err = streams.err ?? process.stderr;
  // Until arguments are parsed, honour --verbose from the raw command line.
  let console = new Console({ out, err, verbose: raw.includes("-v") || raw.includes("--verbose") });
  try {
    let args: Args;
    try {
      args = asArgs(buildParser().parse(raw));
    } catch (error) {
      if (error instanceof ParserExit) {
        out.write(error.text);
        return error.status;
      }
      if (error instanceof ParserError) {
        err.write(error.text);
        return error.status;
      }
      throw error;
    }
    console = new Console({ out, err, verbose: args.verbose, color: args.no_color ? false : null });
    const key = args.command === "key" ? `key ${args.key_command}` : args.command;
    const handler = HANDLERS[key];
    if (handler === undefined) throw new Error(`no handler for ${key}`);
    return await handler(args, console);
  } catch (error) {
    if (error instanceof CliError) {
      console.error(error.message, error.hint);
      return error.exitCode;
    }
    if (error instanceof Interrupted) {
      console.error("Cancelled.");
      return EXIT_INTERRUPTED;
    }
    console.error(
      "Something unexpected went wrong.",
      "Run again with --verbose for details, and please report it if it keeps happening.",
    );
    if (console.verbose) console.detail(error instanceof Error ? (error.stack ?? String(error)) : String(error));
    return EXIT_FAILURE;
  }
}

/** Run as the `modelmux` command. */
export async function cli(): Promise<void> {
  let interrupted = false;
  process.on("SIGINT", () => {
    if (interrupted) process.exit(EXIT_INTERRUPTED); // a second Ctrl+C: stop now
    interrupted = true;
    interrupt();
  });
  process.exitCode = await main();
}
