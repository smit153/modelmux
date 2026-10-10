import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { after, before, describe, it } from "node:test";

import { Parser, ParserError } from "../src/args.ts";
import { buildParser } from "../src/cli.ts";
import { cli, FIXTURES } from "./helpers.ts";

// Each case: fixture name -> command line, as captured from the Python CLI.
const CASES: Record<string, string[]> = {
  help: ["--help"],
  none: [],
  up_help: ["up", "--help"],
  down_help: ["down", "--help"],
  logs_help: ["logs", "--help"],
  status_help: ["status", "--help"],
  login_help: ["login", "--help"],
  logout_help: ["logout", "--help"],
  config_help: ["config", "--help"],
  doctor_help: ["doctor", "--help"],
  upgrade_help: ["upgrade", "--help"],
  key_help: ["key", "--help"],
  keyshow_help: ["key", "show", "--help"],
  key_none: ["key"],
  badcmd: ["frob"],
  login_none: ["login"],
  login_bad: ["login", "foo"],
  config_bad: ["config", "xml"],
  logs_tail: ["logs", "--tail", "x"],
  unknown_opt: ["up", "--frob"],
  extra: ["status", "extra"],
  port_missing: ["up", "--port"],
};

const read = (name: string): string => readFileSync(path.join(FIXTURES, "argparse", name), "utf8");

describe("same help, usage and errors as the Python CLI", () => {
  let columns: string | undefined;
  before(() => {
    columns = process.env.COLUMNS;
    process.env.COLUMNS = "80";
  });
  after(() => {
    if (columns === undefined) delete process.env.COLUMNS;
    else process.env.COLUMNS = columns;
  });

  for (const [name, argv] of Object.entries(CASES)) {
    it(`modelmux ${argv.join(" ")}`.trimEnd(), async () => {
      const { code, out, err } = await cli(...argv);
      assert.equal(out, read(`${name}.out`));
      assert.equal(err, read(`${name}.err`));
      assert.equal(code, Number(read(`${name}.code`).trim()));
    });
  }
});

describe("parser", () => {
  const parse = (...argv: string[]): Record<string, unknown> => buildParser().parse(argv);

  it("--version", async () => {
    const { code, out } = await cli("--version");
    assert.equal(code, 0);
    assert.match(out, /^modelmux \d+\.\d+\.\d+\n$/);
  });

  for (const argv of [["up"], ["down"], ["logs"], ["status"], ["login", "claude"], ["logout", "codex"],
    ["config", "litellm"], ["doctor"], ["upgrade"], ["key", "show"]]) {
    it(`parses ${argv.join(" ")}`, () => {
      assert.equal(parse(...argv).command, argv[0]);
    });
  }

  it("defaults", () => {
    assert.deepEqual(parse("up"), {
      command: "up", verbose: false, no_color: false, providers: [], image: null, port: null,
    });
    const logs = parse("logs");
    assert.equal(logs.tail, 100);
    assert.equal(logs.provider, null);
    assert.equal(logs.follow, false);
  });

  it("global options before the command", () => {
    const args = parse("-v", "--no-color", "status");
    assert.equal(args.verbose, true);
    assert.equal(args.no_color, true);
  });

  it("unique prefixes of long options", () => {
    assert.equal(parse("--verb", "status").verbose, true);
    assert.equal(parse("logs", "--fol").follow, true);
    assert.deepEqual(parse("up", "--po", "claude=9101").port, ["claude=9101"]);
  });

  it("ambiguous prefixes", () => {
    const parser = new Parser("prog");
    parser.addOption({ flags: ["--follow"], action: "store_true" });
    parser.addOption({ flags: ["--force"], action: "store_true" });
    assert.throws(() => parser.parse(["--fo"]), (e: unknown) =>
      e instanceof ParserError && e.text.endsWith("prog: error: ambiguous option: --fo could match --follow, --force\n"));
  });

  it("--opt=value, repeated options and nargs *", () => {
    const args = parse("up", "claude", "codex", "--image=img:1", "--port", "claude=1", "--port=codex=2");
    assert.deepEqual(args.providers, ["claude", "codex"]);
    assert.equal(args.image, "img:1");
    assert.deepEqual(args.port, ["claude=1", "codex=2"]);
  });

  it("short flag clusters", () => {
    const args = parse("logs", "-f", "claude");
    assert.equal(args.follow, true);
    assert.equal(args.provider, "claude");
  });

  it("negative numbers are values", () => {
    assert.equal(parse("logs", "--tail", "-5").tail, -5);
  });

  it("-- ends options", () => {
    assert.equal(parse("login", "--", "claude").provider, "claude");
  });

  it("global options after the command are not accepted (like argparse)", async () => {
    const { code, err } = await cli("status", "-v");
    assert.equal(code, 2);
    assert.match(err, /modelmux: error: unrecognized arguments: -v/);
  });
});
