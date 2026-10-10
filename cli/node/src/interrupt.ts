/**
 * Ctrl+C and friends, as one signal every long-running step can watch.
 *
 * Python turns Ctrl+C into KeyboardInterrupt wherever the program is; Node
 * delivers it as an event. `main` installs the SIGINT handler, and running
 * commands, waits and HTTP polls reject with `Interrupted` when it fires, so
 * cleanup (`finally`, removing the login helper) runs and the user sees
 * "Cancelled.".
 */

import { Interrupted } from "./errors.ts";

let controller = new AbortController();

export function interruptSignal(): AbortSignal {
  return controller.signal;
}

/** Fire the interrupt (SIGINT; SIGTERM/SIGHUP while logging in). */
export function interrupt(): void {
  controller.abort(new Interrupted());
}

/** Start over with a fresh signal (tests). */
export function resetInterrupt(): void {
  controller = new AbortController();
}

export function throwIfInterrupted(): void {
  if (controller.signal.aborted) throw new Interrupted();
}

/** Run `fn`; if the interrupt fires first, reject with `Interrupted`. */
export function interruptible<T>(promise: Promise<T>): Promise<T> {
  const signal = controller.signal;
  if (signal.aborted) return Promise.reject(new Interrupted());
  return new Promise<T>((resolve, reject) => {
    const onAbort = (): void => reject(new Interrupted());
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}
