/**
 * Provider definitions, loaded from the shared JSON files.
 *
 * The files are validated strictly here (no dependency needed); the test suite
 * additionally validates them against `shared/schema/provider.schema.json`.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { CliError, UsageError } from "./errors.ts";
import { repr, sortedStrings } from "./text.ts";

const IDENTIFIER = /^[a-z][a-z0-9-]{0,31}$/;
const VOLUME = /^[a-z0-9][a-z0-9_.-]{0,62}$/;
const ARG = /^[A-Za-z0-9._=/-]+$/;

export interface LoginMethod {
  readonly name: string;
  readonly description: string;
  readonly command: readonly string[];
  readonly secretStdin: string | null;
}

export interface Provider {
  readonly name: string;
  readonly displayName: string;
  readonly driver: string;
  readonly defaultPort: number;
  readonly volume: string;
  readonly home: string;
  readonly exampleModel: string;
  readonly loginMethods: ReadonlyMap<string, LoginMethod>;
  readonly defaultLoginMethod: string;
  readonly linkPattern: RegExp;
  readonly loginTimeout: number;
  readonly statusCommand: readonly string[];
  readonly loggedInExitCode: number;
  readonly logoutCommand: readonly string[];
  readonly docsUrl: string | null;
}

/** The bundled shared data, or the repository's /shared when run from source. */
export function sharedDir(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const bundled = path.join(here, "_shared");
  if (existsSync(bundled) && statSync(bundled).isDirectory()) return bundled;
  return path.resolve(here, "..", "..", "..", "shared"); // cli/node/{src,dist} -> repo root
}

export class ProviderFileError extends CliError {
  constructor(file: string, problem: string) {
    super(`Provider definition ${path.basename(file)} is invalid: ${problem}`, {
      hint: "This is a packaging bug; please report it.",
    });
  }
}

type Json = Record<string, unknown>;
type Kind = "str" | "int" | "dict";

function isDict(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireValue(data: Json, key: string, kind: "str", file: string): string;
function requireValue(data: Json, key: string, kind: "int", file: string): number;
function requireValue(data: Json, key: string, kind: "dict", file: string): Json;
function requireValue(data: Json, key: string, kind: Kind, file: string): unknown {
  const value = data[key];
  const ok =
    kind === "str"
      ? typeof value === "string"
      : kind === "int"
        ? Number.isInteger(value)
        : isDict(value);
  if (!ok) throw new ProviderFileError(file, `'${key}' must be a ${kind}`);
  return value;
}

function command(value: unknown, file: string, where: string): readonly string[] {
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    !value.every((a) => typeof a === "string" && ARG.test(a))
  ) {
    throw new ProviderFileError(file, `'${where}' must be a non-empty list of plain arguments`);
  }
  return Object.freeze([...(value as string[])]);
}

function errorName(error: unknown): string {
  if (error instanceof SyntaxError) return "JSONDecodeError";
  const code = (error as NodeJS.ErrnoException | null)?.code;
  if (code === "ENOENT") return "FileNotFoundError";
  if (code === "EACCES" || code === "EPERM") return "PermissionError";
  if (code === "EISDIR") return "IsADirectoryError";
  return "OSError";
}

function parse(file: string): Provider {
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    throw new ProviderFileError(file, `cannot be read (${errorName(error)})`);
  }
  if (!isDict(data) || data.schema_version !== 1) {
    throw new ProviderFileError(file, "unsupported schema_version");
  }

  const name = requireValue(data, "name", "str", file);
  if (!IDENTIFIER.test(name) || path.basename(file, ".json") !== name) {
    throw new ProviderFileError(file, "'name' must be an identifier matching the file name");
  }
  const volume = requireValue(data, "volume", "str", file);
  if (!VOLUME.test(volume)) throw new ProviderFileError(file, "'volume' is not a valid volume name");
  const home = requireValue(data, "home", "str", file);
  if (!home.startsWith("/")) {
    throw new ProviderFileError(file, "'home' must be an absolute container path");
  }
  const port = requireValue(data, "default_port", "int", file);
  if (port < 1024 || port > 65535) {
    throw new ProviderFileError(file, "'default_port' must be between 1024 and 65535");
  }

  const login = requireValue(data, "login", "dict", file);
  const methods = new Map<string, LoginMethod>();
  for (const [methodName, method] of Object.entries(requireValue(login, "methods", "dict", file))) {
    if (!IDENTIFIER.test(methodName) || !isDict(method)) {
      throw new ProviderFileError(file, `invalid login method ${repr(methodName)}`);
    }
    const secret = method.secret_stdin;
    methods.set(methodName, {
      name: methodName,
      description: requireValue(method, "description", "str", file),
      command: command(method.command, file, `login.methods.${methodName}`),
      secretStdin: typeof secret === "string" ? secret : null,
    });
  }
  const defaultMethod = requireValue(login, "default_method", "str", file);
  if (!methods.has(defaultMethod)) {
    throw new ProviderFileError(file, "'login.default_method' is not one of the methods");
  }
  let linkPattern: RegExp;
  try {
    linkPattern = new RegExp(requireValue(login, "link_pattern", "str", file), "g");
  } catch (error) {
    if (error instanceof ProviderFileError) throw error;
    throw new ProviderFileError(file, "'login.link_pattern' is not a valid regex");
  }

  const status = requireValue(data, "status", "dict", file);
  const logout = requireValue(data, "logout", "dict", file);
  const docsUrl = data.docs_url;
  return Object.freeze({
    name,
    displayName: requireValue(data, "display_name", "str", file),
    driver: requireValue(data, "driver", "str", file),
    defaultPort: port,
    volume,
    home,
    exampleModel: requireValue(data, "example_model", "str", file),
    loginMethods: methods,
    defaultLoginMethod: defaultMethod,
    linkPattern,
    loginTimeout: requireValue(login, "timeout_seconds", "int", file),
    statusCommand: command(status.command, file, "status.command"),
    loggedInExitCode: requireValue(status, "logged_in_exit_code", "int", file),
    logoutCommand: command(logout.command, file, "logout.command"),
    docsUrl: typeof docsUrl === "string" ? docsUrl : null,
  });
}

const cache = new Map<string, ReadonlyMap<string, Provider>>();

/** All providers by name, sorted by name. Cached per directory. */
export function loadProviders(directory?: string): ReadonlyMap<string, Provider> {
  const dir = directory ?? path.join(sharedDir(), "providers");
  const cached = cache.get(dir);
  if (cached) return cached;
  let names: string[] = [];
  try {
    names = sortedStrings(readdirSync(dir).filter((f) => f.endsWith(".json")));
  } catch {
    names = [];
  }
  const providers = new Map<string, Provider>();
  for (const provider of names.map((f) => parse(path.join(dir, f)))) {
    providers.set(provider.name, provider);
  }
  if (providers.size === 0) {
    throw new CliError("No provider definitions found.", {
      hint: "This is a packaging bug; please report it.",
    });
  }
  const volumes = [...providers.values()].map((p) => p.volume);
  if (new Set(volumes).size !== volumes.length) {
    throw new CliError("Two providers share a login volume.", { hint: "Please report this bug." });
  }
  const sorted = new Map(sortedStrings(providers.keys()).map((n) => [n, providers.get(n)!]));
  cache.set(dir, sorted);
  return sorted;
}

export function getProvider(name: string): Provider {
  const providers = loadProviders();
  const provider = providers.get(name);
  if (provider === undefined) {
    throw new UsageError(`Unknown provider ${repr(name)}.`, {
      hint: `Choose one of: ${[...providers.keys()].join(", ")}.`,
    });
  }
  return provider;
}
