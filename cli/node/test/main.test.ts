import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { factory } from "../src/context.ts";
import { DockerError, EXIT_DOCKER, EXIT_INTERRUPTED, EXIT_USAGE, Interrupted } from "../src/errors.ts";
import { cli, useEnv } from "./helpers.ts";

const env = useEnv();

describe("main", () => {
  it("no command is a usage error", async () => {
    assert.equal((await cli()).code, EXIT_USAGE);
  });

  it("unknown provider", async () => {
    const { code, err } = await cli("login", "nope");
    assert.equal(code, EXIT_USAGE);
    assert.match(err, /invalid choice: 'nope'/);
  });

  it("reports a CliError", async () => {
    env.docker.checkAvailableError = new DockerError("Docker is not running.", {
      hint: "Start Docker Desktop.",
    });
    const { code, out, err } = await cli("status");
    assert.equal(code, EXIT_DOCKER);
    assert.equal(out, "");
    assert.match(err, /Docker is not running\./);
    assert.match(err, /Start Docker Desktop\./);
  });

  it("Ctrl+C", async () => {
    factory.makeDocker = () => {
      throw new Interrupted();
    };
    const { code, err } = await cli("status");
    assert.equal(code, EXIT_INTERRUPTED);
    assert.match(err, /Cancelled\./);
  });

  it("an unexpected error hides the details", async () => {
    factory.makeDocker = () => {
      throw new RuntimeError("internal detail");
    };
    const { code, err } = await cli("status");
    assert.equal(code, 1);
    assert.match(err, /Something unexpected went wrong\./);
    assert.doesNotMatch(err, /internal detail/);
  });

  it("--verbose shows the stack trace", async () => {
    factory.makeDocker = () => {
      throw new RuntimeError("internal detail");
    };
    const { code, err } = await cli("--verbose", "status");
    assert.equal(code, 1);
    assert.match(err, /RuntimeError: internal detail/);
    assert.match(err, /at /);
  });
});

class RuntimeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RuntimeError";
  }
}
