/**
 * This Controller, and the one button that replaces it (ADR-0018).
 *
 * Lifecycle and network only; every decision about what the answers *mean* is
 * in `controller.ts`, which is pure.
 *
 * Its own hook rather than a few more fields on `useFleet`, and the reason is
 * the polling: during an update this has to be asked every two seconds and has
 * to **tolerate the answer not arriving**, because the process answering is
 * being stopped and replaced. The fleet poll wants neither of those things,
 * and giving it both would make every unrelated network blip look like an
 * update in progress.
 */

import { useCallback, useEffect, useState } from 'react';
import type { ControllerSelf } from '../../../api/types';
import { isMockMode } from '../../../mock/demoMode';

/** While a run is going. Fast enough that each phase is actually seen. */
const ACTIVE_INTERVAL_MS = 2_000;

/** Otherwise. This answer moves when somebody presses a button, and not before. */
const IDLE_INTERVAL_MS = 30_000;

export interface Controller {
  /** `null` until the first answer. The panel makes no claim before that. */
  readonly self: ControllerSelf | null;
  /**
   * Ask for an update. Returns the reason it could not, or `null` if it began.
   *
   * Returned rather than thrown or stored: every reason is a sentence about
   * this machine — read-only, no updater, a run already going — and it belongs
   * beside the button that was pressed.
   */
  readonly update: (version?: string) => Promise<string | null>;
  /** Forget a finished run, so the panel goes back to offering one. */
  readonly dismiss: () => void;
}

export function useController(baseUrl: string): Controller {
  const [self, setSelf] = useState<ControllerSelf | null>(null);
  const [dismissed, setDismissed] = useState(0);
  const [nonce, setNonce] = useState(0);

  const running = self?.update?.running ?? false;

  useEffect(() => {
    if (isMockMode()) return;

    let disposed = false;
    let timer: number | undefined;
    const interval = running ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;

    const poll = async () => {
      const controller = new AbortController();
      try {
        const response = await fetch(new URL('/api/v1/controller', baseUrl), {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`controller ${response.status}`);
        const answer = (await response.json()) as ControllerSelf;
        if (disposed) return;
        setSelf((previous) => (same(previous, answer) ? previous : answer));
      } catch {
        // **Deliberately swallowed, and this is the whole point of the hook.**
        // A failed fetch during an update is the update: the process that
        // answers this route is stopped, replaced and started again. Setting
        // an `unreachable` flag here would replace a progress bar with an
        // error at the exact moment the operator most needs to be told to
        // wait. The last answer stays on screen, which correctly reads as
        // "still swapping" — and if the new Controller never comes up, the
        // machine rolls itself back and the next successful poll says so.
      } finally {
        if (!disposed) timer = window.setTimeout(poll, interval);
      }
    };

    void poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [baseUrl, running, nonce]);

  const update = useCallback(
    async (version?: string): Promise<string | null> => {
      if (isMockMode()) return 'The demo has no Controller to update.';
      try {
        const response = await fetch(new URL('/api/v1/controller/update', baseUrl), {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ version: version ?? '' }),
        });
        if (!response.ok) {
          const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
          return typeof body?.detail === 'string'
            ? body.detail
            : `The Controller would not start an update. (${response.status})`;
        }
        // Re-poll at once rather than waiting out thirty seconds. Root has not
        // written anything yet, so the first tick after this is what turns the
        // button into a progress bar.
        setDismissed(0);
        setNonce((n) => n + 1);
        return null;
      } catch {
        return 'Could not reach the Controller.';
      }
    },
    [baseUrl],
  );

  // Dismissal is remembered by *when the run last moved*, not by a boolean. A
  // boolean would hide the next run too, and the next run is the one somebody
  // pressed the button for.
  const dismiss = useCallback(() => setDismissed(self?.update?.updated_at ?? 0), [self]);

  const visible =
    self && self.update && self.update.updated_at === dismissed
      ? { ...self, update: null }
      : self;

  return { self: visible, update, dismiss };
}

/**
 * Whether two answers say the same thing.
 *
 * Polled, so a new object identity every tick would re-render the panel — and
 * the app around it — to report that nothing happened. Compared on what is
 * drawn.
 */
function same(before: ControllerSelf | null, after: ControllerSelf): boolean {
  if (before === null) return false;
  return (
    before.version === after.version &&
    before.updatable === after.updatable &&
    before.read_only === after.read_only &&
    before.update?.phase === after.update?.phase &&
    before.update?.version === after.update?.version &&
    before.update?.detail === after.update?.detail &&
    before.update?.updated_at === after.update?.updated_at
  );
}
