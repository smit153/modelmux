import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import type { AddressInfo } from "node:net";
import { describe, it } from "node:test";

import { server as api, waitUntil } from "../src/health.ts";
import { interrupt, resetInterrupt } from "../src/interrupt.ts";
import { ports } from "../src/ports.ts";
import { Interrupted } from "../src/errors.ts";

async function serve(handler: http.RequestListener): Promise<[http.Server, number]> {
  const srv = http.createServer(handler);
  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
  return [srv, (srv.address() as AddressInfo).port];
}

describe("health", () => {
  it("reads JSON and sends the key", async () => {
    let auth: string | undefined;
    const [srv, port] = await serve((req, res) => {
      auth = req.headers.authorization;
      res.writeHead(200, { "content-type": "application/json" }).end('{"status": "ready"}');
    });
    try {
      assert.deepEqual(await api.getJson(port, "/health/ready", { apiKey: "k".repeat(43) }), [200, { status: "ready" }]);
      assert.equal(auth, `Bearer ${"k".repeat(43)}`);
      assert.equal(await api.ready(port), true);
    } finally {
      srv.close();
    }
  });

  it("keeps the status of an HTTP error", async () => {
    const [srv, port] = await serve((_req, res) => res.writeHead(401).end('{"error": "no"}'));
    try {
      assert.deepEqual(await api.getJson(port, "/v1/models"), [401, { error: "no" }]);
      assert.equal(await api.ready(port), false);
    } finally {
      srv.close();
    }
  });

  it("unreachable is status 0", async () => {
    const [srv, port] = await serve(() => undefined);
    srv.close();
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(await api.getJson(port, "/health/ready"), [0, null]);
  });

  it("a server that never answers times out", async () => {
    const [srv, port] = await serve(() => undefined);
    try {
      assert.deepEqual(await api.getJson(port, "/", { timeout: 0.05 }), [0, null]);
    } finally {
      srv.closeAllConnections();
      srv.close();
    }
  });

  it("waitUntil polls until true, stops early, or times out", async () => {
    let n = 0;
    assert.equal(await waitUntil(() => ++n >= 3, { timeout: 5, interval: 0 }), true);
    assert.equal(n, 3);
    assert.equal(await waitUntil(() => false, { timeout: 5, interval: 0, shouldStop: () => true }), false);
    assert.equal(await waitUntil(() => false, { timeout: 0, interval: 0 }), false);
  });

  it("Ctrl+C ends a wait", async () => {
    resetInterrupt();
    setTimeout(interrupt, 20);
    await assert.rejects(waitUntil(() => false, { timeout: 60, interval: 0.01 }), Interrupted);
    resetInterrupt();
  });
});

describe("ports", () => {
  it("a port with a listener is not free", async () => {
    const srv = net.createServer();
    await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
    const port = (srv.address() as AddressInfo).port;
    try {
      assert.equal(await ports.portFree(port), false);
    } finally {
      srv.close();
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(await ports.portFree(port), true);
  });
});
