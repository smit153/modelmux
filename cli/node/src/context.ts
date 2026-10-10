/** Everything a command needs, built in one place (and replaceable in tests). */

import { type Config, loadConfig } from "./config.ts";
import type { Console } from "./console.ts";
import { Docker } from "./docker.ts";
import { configDir } from "./paths.ts";
import { loadProviders, type Provider } from "./providers.ts";
import { Stack } from "./stack.ts";

/** Replaceable in tests: how commands get their Docker. */
export const factory = {
  makeDocker: (console: Console): Docker => new Docker(console),
};

export class Context {
  readonly console: Console;
  readonly docker: Docker;
  readonly home: string;
  private cachedConfig: Config | null = null;
  private cachedStack: Stack | null = null;

  constructor(console: Console, docker: Docker, home: string = configDir()) {
    this.console = console;
    this.docker = docker;
    this.home = home;
  }

  get config(): Config {
    this.cachedConfig ??= loadConfig(this.home);
    return this.cachedConfig;
  }

  get providers(): ReadonlyMap<string, Provider> {
    return loadProviders();
  }

  get stack(): Stack {
    this.cachedStack ??= new Stack(this.docker, this.home);
    return this.cachedStack;
  }
}

export function makeContext(console: Console): Context {
  return new Context(console, factory.makeDocker(console));
}
