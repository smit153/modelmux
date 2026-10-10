import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import { Console } from "../src/console.ts";
import { clearSecrets, MASK, redact, registerSecret } from "../src/redact.ts";
import { Capture } from "./helpers.ts";

function make(options: { tty?: boolean; unicode?: boolean } = {}): [Console, Capture, Capture] {
  const out = new Capture();
  const err = new Capture();
  out.isTTY = err.isTTY = options.tty ?? false;
  return [new Console({ out, err, unicode: options.unicode ?? true }), out, err];
}

function withEnv(values: Record<string, string | undefined>, fn: () => void): void {
  const saved = Object.fromEntries(Object.keys(values).map((k) => [k, process.env[k]]));
  try {
    for (const [k, v] of Object.entries(values)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

describe("console", () => {
  beforeEach(() => clearSecrets());

  it("sends messages to the right stream", () => {
    const [console, out, err] = make();
    console.success("done");
    console.step("working");
    console.print("plain");
    console.warn("careful");
    console.error("broken", "try this");
    assert.equal(out.text, "✓ done\n→ working\nplain\n");
    assert.equal(err.text, "! careful\n✗ broken\n  try this\n");
  });

  it("no colour when not a terminal", () => {
    const [console, out] = make({ tty: false });
    console.success("x");
    assert.ok(!out.text.includes("\x1b["));
  });

  it("colour on a terminal", () => {
    withEnv({ NO_COLOR: undefined, TERM: "xterm" }, () => {
      const [console, out] = make({ tty: true });
      console.success("x");
      assert.ok(out.text.includes("\x1b[32m"));
    });
  });

  for (const [name, value] of [["NO_COLOR", "1"], ["TERM", "dumb"]] as const) {
    it(`${name}=${value} disables colour`, () => {
      withEnv({ [name]: value }, () => {
        const [console, out] = make({ tty: true });
        console.success("x");
        assert.ok(!out.text.includes("\x1b["));
      });
    });
  }

  it("ASCII fallback", () => {
    const [console, out, err] = make({ unicode: false });
    console.success("done");
    console.error("bad");
    assert.equal(out.text, "OK done\n");
    assert.equal(err.text, "X bad\n");
  });

  it("details only when verbose", () => {
    const out = new Capture();
    const err = new Capture();
    new Console({ out, err }).detail("hidden");
    assert.equal(err.text, "");
    new Console({ out, err, verbose: true }).detail("shown");
    assert.ok(err.text.includes("shown"));
  });

  it("redacts output", () => {
    registerSecret("my-generated-server-key-123456");
    const [console, out, err] = make();
    console.print("key=my-generated-server-key-123456");
    console.error("Authorization: Bearer abc.def.ghi");
    console.detail("MODELMUX_API_KEYS=whatever");
    assert.ok(!out.text.includes("my-generated-server-key"));
    assert.ok(out.text.includes(MASK));
    assert.ok(!err.text.includes("abc.def"));
  });

  it("reveal is never redacted", () => {
    registerSecret("my-generated-server-key-123456");
    const [console, out] = make();
    console.reveal("my-generated-server-key-123456");
    assert.equal(out.text, "my-generated-server-key-123456\n");
  });
});

describe("redact", () => {
  beforeEach(() => clearSecrets());

  for (const text of [
    "sk-ant-api03-abcdefghijklmnop",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_value-1",
    "MODELMUX_API_KEYS=abcdefghijklmnop1234",
    "bearer abc.def",
  ]) {
    it(`masks ${text.slice(0, 12)}...`, () => {
      assert.ok(redact(`x ${text} y`).includes(MASK));
    });
  }

  it("does not register short values", () => {
    registerSecret("abc");
    assert.equal(redact("abc"), "abc");
  });

  it("leaves hints alone", () => {
    const hint = "export MODELMUX_API_KEY=$(modelmux key show)";
    assert.equal(redact(hint), hint);
  });
});
