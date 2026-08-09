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
 * **It streams rather than polls.** Opening the panel subscribes to
 * `/graph/node/logs/stream`, which backfills a tail and then follows. A poll
 * was rejected on the way here and the reason is worth keeping: it would
 * re-send the whole tail every few seconds whether or not anything had been
 * written, which is the worst backpressure story available — most expensive
 * exactly when the container is quiet and nothing has changed.
 *
 * **Server-sent events, not a WebSocket.** The data goes one way and the
 * cancellation signal is the connection closing, which is what `EventSource`
 * gives for free. It is also an ordinary GET, so the reverse proxy someone may
 * have in front of the browser port already understands it.
 *
 * **It does not remember the previous container's output.** Carrying lines
 * across a selection change would attribute one workload's crash to another,
 * which is the single most expensive mistake this panel could make.
 *
 * **It does not say how many lines it wants.** `tail` is omitted, so the
 * Controller's default applies — one number, on the side that owns the bound
 * and already publishes it in the OpenAPI schema, instead of a copy here that
 * nothing keeps in step. The footer counts the lines that arrived rather than
 * repeating what was asked for, so it stays true whatever that default is.
 */

import { useCallback, useEffect, useState } from 'react';
import type { LogLine, Urn } from '../../../api/types';
import { appendLive, type LogsState } from './logs';

export interface Logs {
  readonly state: LogsState;
  readonly open: boolean;
  readonly toggle: () => void;
  readonly refresh: () => void;
}

export function useLogs(baseUrl: string, target: Urn | null): Logs {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<LogsState>({ kind: 'idle' });
  // Bumped by Refresh, which now means "reopen the stream" — the effect below
  // owns the only `EventSource`, so re-running it is the one path that is
  // guaranteed to close the previous one before opening another.
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

    setState({ kind: 'loading' });

    const url = new URL('/api/v1/graph/node/logs/stream', baseUrl);
    url.searchParams.set('urn', target);
    const source = new EventSource(url);

    source.addEventListener('lines', (event) => {
      const chunk = JSON.parse((event as MessageEvent<string>).data) as {
        lines: LogLine[];
        dropped: number;
      };
      setState((previous) => {
        // Anything that is not already a live tail becomes one here: the
        // first chunk is what turns `loading` into `live`.
        const lines = previous.kind === 'live' ? previous.lines : [];
        const dropped = previous.kind === 'live' ? previous.dropped : 0;
        return {
          kind: 'live',
          lines: appendLive(lines, chunk.lines),
          dropped: dropped + chunk.dropped,
          ended: null,
        };
      });
    });

    source.addEventListener('end', (event) => {
      const { reason } = JSON.parse((event as MessageEvent<string>).data) as {
        reason: string | null;
      };
      // The server said this stream is over, so stop here rather than letting
      // EventSource do what it does by default and reconnect. A container that
      // exited would otherwise be re-followed every few seconds forever.
      source.close();
      setState((previous) => ({
        kind: 'live',
        lines: previous.kind === 'live' ? previous.lines : [],
        dropped: previous.kind === 'live' ? previous.dropped : 0,
        ended: { reason },
      }));
    });

    source.onerror = () => {
      // `EventSource` reports the refusal and the dropped connection through
      // the same event and does not expose the status code, so this cannot
      // distinguish "the Controller said no" from "the network went". Say the
      // honest thing rather than guessing at one of them.
      //
      // Only when nothing has arrived yet: mid-stream, the browser retries by
      // itself and replacing a screen of real output with an error would be a
      // worse answer than a pause.
      setState((previous) =>
        previous.kind === 'live'
          ? previous
          : { kind: 'error', message: 'Could not open the log stream.' },
      );
    };

    return () => source.close();
  }, [baseUrl, target, open, nonce]);

  const toggle = useCallback(() => setOpen((value) => !value), []);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return { state, open, toggle, refresh };
}
