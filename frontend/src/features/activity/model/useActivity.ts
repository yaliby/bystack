/**
 * The operations timeline.
 *
 * Lifecycle and network only; what an entry *means* is in `activity.ts`.
 *
 * Polled, like the fleet, and for the same reason: operations are not on the
 * delta stream. They deliberately are not — a command does not change the
 * graph, discovery does, a moment later (ARCHITECTURE §9) — so the act of
 * running one produces no event a browser could subscribe to. What it does
 * produce, eventually, is the state change itself, on the stream, which is the
 * part that matters and already arrives.
 *
 * Only while the panel is open. The log is written by operators, at human
 * speed, and a closed panel is nobody watching.
 */

import { useCallback, useEffect, useState } from 'react';
import type { AuditEntry } from '../../../api/types';
import { sameActivity } from './activity';

const INTERVAL_MS = 5_000;

/**
 * How many entries to ask for.
 *
 * The ring is bounded on the Controller and this is a panel, not an export.
 * Fifty is more than fits on a screen and far less than the ceiling the route
 * allows, so an operator scrolling back sees where the window ends rather than
 * waiting on a response that grew with the fleet.
 */
const LIMIT = 50;

export interface Activity {
  readonly entries: readonly AuditEntry[];
  /** False until the first answer, so the panel says "loading" once. */
  readonly loaded: boolean;
  /** The Controller did not answer. Distinct from nothing having happened. */
  readonly unreachable: boolean;
  /** Refetch now, without waiting out the interval. */
  readonly refresh: () => void;
}

export function useActivity(baseUrl: string, open: boolean): Activity {
  const [entries, setEntries] = useState<readonly AuditEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [unreachable, setUnreachable] = useState(false);
  // Bumped when a command is run from this browser. Waiting five seconds to
  // see your own action appear reads as a click that missed.
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!open) return;
    let disposed = false;
    let timer: number | undefined;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const response = await fetch(
          new URL(`/api/v1/commands/audit?limit=${LIMIT}`, baseUrl),
          { signal: controller.signal },
        );
        if (!response.ok) throw new Error(`audit ${response.status}`);
        const next = (await response.json()) as AuditEntry[];
        if (disposed) return;
        setUnreachable(false);
        setLoaded(true);
        setEntries((previous) => (sameActivity(previous, next) ? previous : next));
      } catch {
        if (!disposed && !controller.signal.aborted) setUnreachable(true);
      } finally {
        if (!disposed) timer = window.setTimeout(poll, INTERVAL_MS);
      }
    };

    void poll();

    return () => {
      disposed = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [baseUrl, open, nonce]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return { entries, loaded, unreachable, refresh };
}
