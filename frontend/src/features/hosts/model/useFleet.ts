/**
 * The enrolled fleet, and the three operator actions on it.
 *
 * Lifecycle and network only — every decision about what the answers *mean*
 * is in `hosts.ts`, which is pure.
 *
 * Polled, like `useHealth`, and for the same reason: enrollment is not on the
 * delta stream. Nothing an agent does before it is approved touches the graph
 * — that is the entire point of the pending state (ADR-0011) — so the one
 * moment this panel exists for, a new host appearing, produces no event a
 * browser could subscribe to.
 */

import { useCallback, useEffect, useState } from 'react';
import type { EnrolledAgent, EnrollmentTerms, JoinToken } from '../../../api/types';
import { sameFleet, sameTerms } from './hosts';

/**
 * Two cadences, because the panel has two jobs.
 *
 * Open, it is being watched by someone who just pasted a command onto another
 * machine and is waiting for the row; four seconds is the difference between
 * "it worked" and "did it work?". Closed, it only feeds the pending count on
 * the toggle, which nobody is staring at.
 */
const ATTENTIVE_INTERVAL_MS = 4_000;
const BACKGROUND_INTERVAL_MS = 20_000;

/** The Controller's own default, sent explicitly so the UI states its intent. */
const TOKEN_TTL_MINUTES = 15;

export interface Fleet {
  readonly agents: readonly EnrolledAgent[];
  /** False until the first answer, so the panel can say "loading" once and only once. */
  readonly loaded: boolean;
  /** The Controller did not answer. Distinct from an empty fleet. */
  readonly unreachable: boolean;
  /**
   * Whether a host can join at all, and whether this machine is managing
   * itself. `null` until fetched — the panel then makes no claim about the
   * listener rather than guessing it is open, which would produce an install
   * command that silently cannot connect.
   *
   * Polled with the list rather than fetched once, which it used to be. The
   * listener's posture really is configuration and does not move without a
   * restart, but `local_agent` is a running process: it fails, backs off and
   * retries, and a first-run panel that fetched its state once would show the
   * reason from three seconds after startup for as long as the tab was open.
   */
  readonly terms: EnrollmentTerms | null;
  /** The last failed action, in the Controller's own words where it gave any. */
  readonly error: string | null;
  /** The engine id of an action in flight, so one row can be busy without the rest. */
  readonly busy: string | null;
  readonly approve: (engineId: string) => Promise<void>;
  readonly revoke: (engineId: string) => Promise<void>;
  /**
   * Mint a join token.
   *
   * Returns it rather than storing it. The Controller keeps only a digest, so
   * this value is unrecoverable the moment it is dropped — which makes where
   * it lives a real decision, and the answer is "in the dialog that shows it,
   * for as long as that dialog is open". Nothing here holds it, and nothing
   * anywhere writes it to storage.
   */
  readonly mint: () => Promise<JoinToken | null>;
  readonly dismissError: () => void;
}

export function useFleet(baseUrl: string, attentive: boolean): Fleet {
  const [agents, setAgents] = useState<readonly EnrolledAgent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [unreachable, setUnreachable] = useState(false);
  const [terms, setTerms] = useState<EnrollmentTerms | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Bumped by an action to re-poll at once rather than waiting out the
  // interval. Approving a host and watching the row not change for four
  // seconds reads as a click that missed.
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    const inFlight = new Set<AbortController>();
    const interval = attentive ? ATTENTIVE_INTERVAL_MS : BACKGROUND_INTERVAL_MS;

    const poll = async () => {
      const controller = new AbortController();
      inFlight.add(controller);
      try {
        const [listed, terms] = await Promise.all([
          fetch(new URL('/api/v1/agents', baseUrl), { signal: controller.signal }),
          fetch(new URL('/api/v1/agents/enrollment', baseUrl), { signal: controller.signal }),
        ]);
        if (!listed.ok) throw new Error(`agents ${listed.status}`);
        const next = (await listed.json()) as EnrolledAgent[];
        if (disposed) return;
        setUnreachable(false);
        setLoaded(true);
        // Publish only a change, as `useHealth` does: a new array identity
        // every four seconds would re-render the panel — and the app around
        // it — to say that nothing happened.
        setAgents((previous) => (sameFleet(previous, next) ? previous : next));

        if (terms.ok) {
          const answer = (await terms.json()) as EnrollmentTerms;
          setTerms((previous) => (sameTerms(previous, answer) ? previous : answer));
        }
      } catch {
        if (!disposed && !controller.signal.aborted) setUnreachable(true);
      } finally {
        inFlight.delete(controller);
        if (!disposed) timer = window.setTimeout(poll, interval);
      }
    };

    void poll();

    return () => {
      disposed = true;
      window.clearTimeout(timer);
      for (const controller of inFlight) controller.abort();
    };
  }, [baseUrl, attentive, nonce]);

  const act = useCallback(
    async (engineId: string, verb: 'approve' | 'revoke') => {
      setBusy(engineId);
      setError(null);
      try {
        const response = await fetch(
          new URL(`/api/v1/agents/${encodeURIComponent(engineId)}/${verb}`, baseUrl),
          { method: 'POST' },
        );
        if (!response.ok) {
          setError(await detailOf(response, `The Controller refused to ${verb} this host.`));
          return;
        }
        // The list is refetched rather than the answer being applied: approve
        // and revoke report the record they just wrote, and only `GET /agents`
        // computes `connected` from the live provider set. Patching the row
        // from the response would have a revoked host that is still streaming
        // report itself as offline.
        setNonce((n) => n + 1);
      } catch {
        setError('Could not reach the Controller.');
      } finally {
        setBusy(null);
      }
    },
    [baseUrl],
  );

  const approve = useCallback((engineId: string) => act(engineId, 'approve'), [act]);
  const revoke = useCallback((engineId: string) => act(engineId, 'revoke'), [act]);

  const mint = useCallback(async (): Promise<JoinToken | null> => {
    setError(null);
    try {
      const response = await fetch(new URL('/api/v1/agents/tokens', baseUrl), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ttl_minutes: TOKEN_TTL_MINUTES }),
      });
      if (!response.ok) {
        setError(await detailOf(response, 'The Controller would not mint a token.'));
        return null;
      }
      return (await response.json()) as JoinToken;
    } catch {
      setError('Could not reach the Controller.');
      return null;
    }
  }, [baseUrl]);

  const dismissError = useCallback(() => setError(null), []);

  return { agents, loaded, unreachable, terms, error, busy, approve, revoke, mint, dismissError };
}

/** FastAPI's `{"detail": …}`, when there is one worth showing. */
async function detailOf(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === 'string' ? body.detail : `${fallback} (${response.status})`;
}
