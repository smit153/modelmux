/** The command line: the same commands, options and help as the Python CLI. */

import type { Namespace } from "./args.ts";
import { Parser } from "./args.ts";
import { TARGETS } from "./clientconfig.ts";
import { loadProviders } from "./providers.ts";
import { VERSION } from "./version.ts";

export function buildParser(): Parser {
  const providers = [...loadProviders().keys()];
  const parser = new Parser("modelmux", {
    description: "Run AI coding CLIs as a secure, OpenAI-compatible API.",
    epilog: "Start with: modelmux up, then modelmux login <provider>.",
  });
  parser.addOption({ flags: ["--version"], action: "version", version: `modelmux ${VERSION}`,
    help: "show program's version number and exit" });
  parser.addOption({ flags: ["-v", "--verbose"], action: "store_true", help: "show details and commands" });
  parser.addOption({ flags: ["--no-color"], action: "store_true", help: "disable coloured output" });
  parser.addSubcommands("command", "<command>");

  parser.addCommand("up", "start ModelMux (logged-in providers)", (up) => {
    up.addPositional({ dest: "providers", nargs: "*", metavar: "provider",
      help: `any of: ${providers.join(", ")}` });
    up.addOption({ flags: ["--image"], metavar: "REF", help: "server image to run (remembered)" });
    up.addOption({ flags: ["--port"], action: "append", metavar: "PROVIDER=PORT",
      help: "local port (remembered)" });
  });

  parser.addCommand("down", "stop ModelMux (logins are kept)");

  parser.addCommand("logs", "show server logs", (logs) => {
    logs.addPositional({ dest: "provider", nargs: "?", choices: providers });
    logs.addOption({ flags: ["-f", "--follow"], action: "store_true", help: "keep streaming new lines" });
    logs.addOption({ flags: ["--tail"], type: "int", default: 100, metavar: "N",
      help: "lines to show (100)" });
  });

  parser.addCommand("status", "what is running, logged in and healthy");

  parser.addCommand("login", "log a provider in (guided)", (login) => {
    login.addPositional({ dest: "provider", choices: providers });
    login.addOption({ flags: ["--method"], help: "login method (see the provider's options)" });
    login.addOption({ flags: ["--no-browser"], action: "store_true", help: "do not open a browser" });
    login.addOption({ flags: ["--raw"], action: "store_true", help: "show the provider's raw output" });
    login.addOption({ flags: ["--force"], action: "store_true", help: "log in again even if logged in" });
  });

  parser.addCommand("logout", "remove a provider's saved login", (logout) => {
    logout.addPositional({ dest: "provider", choices: providers });
    logout.addOption({ flags: ["-y", "--yes"], action: "store_true", help: "do not ask for confirmation" });
  });

  parser.addCommand("config", "print ready-to-paste client config", (config) => {
    config.addPositional({ dest: "target", choices: TARGETS });
    config.addOption({ flags: ["--provider"], choices: providers });
    config.addOption({ flags: ["--reveal-key"], action: "store_true", help: "include the real API key" });
  });

  parser.addCommand("doctor", "diagnose common problems");

  parser.addCommand("upgrade", "run the image matching this CLI version", (upgrade) => {
    upgrade.addOption({ flags: ["--image"], metavar: "REF", help: "use a different image (advanced)" });
  });

  parser.addCommand("key", "the API key clients use to call ModelMux", (key) => {
    key.addSubcommands("key_command", "<action>");
    key.addCommand("show", "print the API key");
  });
  return parser;
}

/** Typed view of the parsed command line. */
export interface Args {
  command: string;
  key_command?: string;
  verbose: boolean;
  no_color: boolean;
  providers: string[];
  image: string | null;
  port: string[] | null;
  provider: string | null;
  follow: boolean;
  tail: number;
  method: string | null;
  no_browser: boolean;
  raw: boolean;
  force: boolean;
  yes: boolean;
  target: string;
  reveal_key: boolean;
}

export function asArgs(namespace: Namespace): Args {
  return namespace as unknown as Args;
}
