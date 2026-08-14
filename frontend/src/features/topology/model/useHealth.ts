/**
 * Controller health subscription.
 *
 * The WebSocket tells us whether *we* are connected to the Controller. It says
 * nothing about whether the Controller can still see the infrastructure, and
 * conflating the two is how a dashboard ends up displaying a confident `live`
 * over a canvas whose provider died ten minutes ago. `/healthz` is the only
 * thing that knows, so we ask it.
 *
 * Polled, deliberately: this is the one place the "no polling where an event
 * stream exists" rule does not apply, because no event stream exists for the
 * control plane's own liveness — and a health channel that only reports when
 * it is well cannot report that it is gone.
 */

import { useEffect, useState } from 'react';
import type { Health } from '../../../api/types';

/** Slow enough to be free, fast enough that a dead provider is not news. */
const POLL_INTERVAL_MS = 10_000;

export type HealthState =
  | { readonly kind: 'pending' }
  /** The Controller answered. `health.status` says whether its providers are well. */
  | { readonly kind: 'reached'; readonly health: Health }
  /** The Controller did not answer at all — a different failure from `degraded`. */
  | { readonly kind: 'unreachable' };

export function useHealth(baseUrl: string): HealthState {
  const [state, setState] = useState<HealthState>({ kind: 'pending' });

  useEffect(() => {
    // Dev mock canvas — invent a healthy Controller so the banner stays quiet.
    if (
      import.meta.env.DEV &&
      new URLSearchParams(window.location.search).get('mock') !== '0'
    ) {
      setState({
        kind: 'reached',
        health: {
          status: 'ok',
          version: 'mock',
          seq: 1,
          node_count: 0,
          edge_count: 0,
          read_only: true,
          providers: [],
        },
      });
      return;
    }

    let disposed = false;
    let timer: number | undefined;
    const controllers = new Set<AbortController>();

    const poll = async () => {
      const controller = new AbortController();
      controllers.add(controller);
      try {
        const response = await fetch(new URL('/api/v1/healthz', baseUrl), {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`healthz ${response.status}`);
        const health = (await response.json()) as Health;
        // Only publish a change. A new object every 10s would re-render the
        // whole app — including a canvas someone may be mid-drag on — to say
        // that nothing happened.
        if (!disposed) setState((previous) => (samePosture(previous, health) ? previous : { kind: 'reached', health }));
      } catch {
        // An aborted request is teardown, not a failure. Anything else means
        // we genuinely could not reach the Controller.
        if (!disposed && !controller.signal.aborted) setState({ kind: 'unreachable' });
      } finally {
        controllers.delete(controller);
        if (!disposed) timer = window.setTimeout(poll, POLL_INTERVAL_MS);
      }
    };

    void poll();

    return () => {
      disposed = true;
      window.clearTimeout(timer);
      for (const controller of controllers) controller.abort();
    };
  }, [baseUrl]);

  return state;
}

/**
 * Whether two answers say the same thing about the system's posture.
 *
 * Deliberately ignores `seq`, counters and `last_sync_at`: those tick on a
 * healthy system doing nothing interesting, and treating them as news would
 * defeat the point of comparing at all.
 */
function samePosture(previous: HealthState, next: Health): boolean {
  if (previous.kind !== 'reached') return false;
  const before = previous.health;
  if (before.status !== next.status || before.read_only !== next.read_only) return false;
  if (before.providers.length !== next.providers.length) return false;
  return before.providers.every((provider, index) => {
    const other = next.providers[index];
    return (
      provider.id === other.id &&
      provider.state === other.state &&
      provider.detail === other.detail
    );
  });
}
