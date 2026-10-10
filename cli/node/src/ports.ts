/** Is a local port free? (Checked with a socket bind: no extra processes.) */

import net from "node:net";

import { LOCALHOST } from "./health.ts";

/**
 * True unless another program is listening on `host:port`.
 *
 * Ports in TIME_WAIT (for example right after `modelmux down`) count as free,
 * as they do for Docker: on POSIX Node binds with `SO_REUSEADDR`, which
 * ignores TIME_WAIT but still fails against a live listener. On Windows Node
 * binds exclusively, so a port in use is never "stolen".
 */
function isFree(port: number, host: string = LOCALHOST): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

/** Replaceable in tests. */
export const ports = {
  portFree: isFree,
};
