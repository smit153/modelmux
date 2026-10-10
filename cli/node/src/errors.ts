/**
 * Errors the CLI reports to the user.
 *
 * Every failure is a `CliError`: a plain message saying what went wrong, an
 * optional hint saying what to do next, and an exit code. `main` is the only
 * place that prints them.
 */

export const EXIT_OK = 0;
export const EXIT_FAILURE = 1;
export const EXIT_USAGE = 2;
export const EXIT_DOCKER = 3;
export const EXIT_INTERRUPTED = 130;

export class CliError extends Error {
  readonly hint: string | null;

  constructor(message: string, options: { hint?: string | null } = {}) {
    super(message);
    this.name = new.target.name;
    this.hint = options.hint ?? null;
  }

  get exitCode(): number {
    return EXIT_FAILURE;
  }
}

/** The command was used incorrectly. */
export class UsageError extends CliError {
  override get exitCode(): number {
    return EXIT_USAGE;
  }
}

/** Docker is missing, stopped, or a Docker command failed. */
export class DockerError extends CliError {
  override get exitCode(): number {
    return EXIT_DOCKER;
  }
}

/** Ctrl+C, SIGTERM or a closed terminal (Python's KeyboardInterrupt). */
export class Interrupted extends Error {
  constructor() {
    super("interrupted");
    this.name = "Interrupted";
  }
}

/** A deadline passed (Python's TimeoutError). */
export class Timeout extends Error {
  constructor() {
    super("timed out");
    this.name = "Timeout";
  }
}
