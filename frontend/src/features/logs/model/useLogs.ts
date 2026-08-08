/**
 * Reading the selected container's log.
 *
 * Lifecycle and network only; every decision about what an answer *means* is
 * in `logs.ts`, which is pure.
 *
 * Three decisions worth naming, because each is a thing this deliberately
 * does not do:
 *
 * **It does not fetch on selection.** Every read goes over the wire to an
 * agent on someone's home uplink and back. Clicking through a canvas would
 * issue one per card, on hosts that are being paid for in bandwidth, for
 * output nobody asked to see. The panel is opened, and opening it is the ask.
 *
 * **It does not poll.** The route is one shot by design (`LogsRequest` has no
 * follow), and a poll would be a stream with the worst possible backpressure
 * story — a full tail re-sent every few seconds whether or not anything was
 * written. Following a live log is a separate feature and this is not its
 * first half.
 *
 * **It does not remember the previous container's output.** Carrying lines
 * across a selection change would attribute one workload's crash to another,
 * which is the single most expensive mistake this panel could make.
 */

import { useCallback, useEffect, useState } from 'react';
import type { ContainerLogs, Urn } from '../../../api/types';
import type { LogsState } from './logs';

/**
 * Lines to ask for. The Controller bounds this at 2000 and the agent again.
 *
 * **KNOWN GAP — `docs/OPEN-WORK.md` §3.4.** This mirrors `DEFAULT_LOG_TAIL`
 * on the Controller with nothing keeping the two in step, and the panel's
 * "Last N lines" footer would go on claiming 200 if the Controller's default
 * moved. The cheapest fix is deleting this: omit `tail` and take the
 * Controller's answer, which is the side that owns the bound.
 */
const TAIL = 200;

export interface Logs {
  readonly state: LogsState;
  readonly open: boolean;
  readonly toggle: () => void;
  readonly refresh: () => void;
}

export function useLogs(baseUrl: string, target: Urn | null): Logs {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<LogsState>({ kind: 'idle' });
  // Bumped by Refresh. A nonce rather than calling fetch directly, so that
  // every read goes through the one effect that owns the AbortController —
  // two paths into the same request is how a cancelled read lands anyway.
  const [nonce, setNonce] = useState(0);

  // Selecting something else closes the panel and drops what was read. Not
  // merely cleared: leaving it open would fire a read at every container the
  // operator clicks through on their way to the one they want.
  useEffect(() => {
    setOpen(false);
    setState({ kind: 'idle' });
  }, [target]);

  useEffect(() => {
    if (!open || !target) return;

    const controller = new AbortController();
    setState({ kind: 'loading' });

    void (async () => {
      try {
        const url = new URL('/api/v1/graph/node/logs', baseUrl);
        url.searchParams.set('urn', target);
        url.searchParams.set('tail', String(TAIL));
        const response = await fetch(url, { signal: controller.signal });

        if (!response.ok) {
          // A refusal the *Controller* made — an unknown node, a host with no
          // agent at all. A host whose agent is merely asleep comes back 200
          // with a reason and is handled as an answer, because it is one.
          setState({ kind: 'error', message: await detailOf(response) });
          return;
        }
        setState({ kind: 'ready', logs: (await response.json()) as ContainerLogs });
      } catch {
        if (!controller.signal.aborted) {
          setState({ kind: 'error', message: 'Could not reach the Controller.' });
        }
      }
    })();

    return () => controller.abort();
  }, [baseUrl, target, open, nonce]);

  const toggle = useCallback(() => setOpen((value) => !value), []);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return { state, open, toggle, refresh };
}

/** FastAPI's `{"detail": …}`, when there is one worth showing. */
async function detailOf(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === 'string'
    ? body.detail
    : `The Controller would not answer (${response.status}).`;
}
