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
import type {
  AgentReleases,
  EnrolledAgent,
  EnrollmentTerms,
  JoinToken,
  Rollout,
} from '../../../api/types';
import { isMockMode } from '../../../mock/demoMode';
import { demoAgents, demoTerms } from '../../../mock/demoWatch';
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
  /**
   * Signed releases this Controller can push, and who could take one.
   *
   * `null` until fetched, which the panel reads as "make no claim": offering
   * an upgrade button before the answer arrives would offer one on a
   * Controller that holds nothing, and the fallback — run the installer on
   * each host — is the *correct* instruction in that case rather than a
   * degraded one.
   */
  readonly releases: AgentReleases | null;
  /** The current or most recent rollout. `null` until one has been started. */
  readonly rollout: Rollout | null;
  /** Start one. Returns the reason it could not, or `null` if it began. */
  readonly upgrade: (version?: string) => Promise<string | null>;
  /** Stop after the current host. Never mid-transfer. */
  readonly stopUpgrade: () => Promise<void>;
  /** Forget a finished run, so the panel goes back to the fleet. */
  readonly dismissRollout: () => void;
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
  const [agents, setAgents] = useState<readonly EnrolledAgent[]>(() =>
    isMockMode() ? demoAgents() : [],
  );
  const [loaded, setLoaded] = useState(() => isMockMode());
  const [unreachable, setUnreachable] = useState(false);
  const [terms, setTerms] = useState<EnrollmentTerms | null>(() =>
    isMockMode() ? demoTerms() : null,
  );
  const [releases, setReleases] = useState<AgentReleases | null>(null);
  const [rollout, setRollout] = useState<Rollout | null>(null);
  const [dismissed, setDismissed] = useState<number>(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Bumped by an action to re-poll at once rather than waiting out the
  // interval. Approving a host and watching the row not change for four
  // seconds reads as a click that missed.
  const [nonce, setNonce] = useState(0);

  //: Whether a rollout is in flight. Read here rather than inside the effect
  //: because it is what decides the cadence, and an effect that read it from
  //: state without depending on it would keep the slow one for the whole run.
  const running = rollout?.state === 'running';

  useEffect(() => {
    if (isMockMode()) return;

    let disposed = false;
    let timer: number | undefined;
    const inFlight = new Set<AbortController>();
    // A run in flight is watched at the attentive cadence whatever the panel
    // is doing, because it is the one thing here that changes on its own and
    // the operator who started it may have closed the panel to look at the
    // map. Four seconds against a rollout measured in minutes is cheap.
    const interval = attentive || running ? ATTENTIVE_INTERVAL_MS : BACKGROUND_INTERVAL_MS;

    const poll = async () => {
      const controller = new AbortController();
      inFlight.add(controller);
      try {
        const [listed, termsResponse] = await Promise.all([
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

        if (termsResponse.ok) {
          const answer = (await termsResponse.json()) as EnrollmentTerms;
          setTerms((previous) => (sameTerms(previous, answer) ? previous : answer));
        }

        // Fetched alongside rather than on their own timer. A rollout moves
        // the version on each card, so the two answers have to arrive close
        // together or the panel spends a poll interval showing a run that has
        // finished above a fleet that has not caught up.
        const [held, run] = await Promise.all([
          fetch(new URL('/api/v1/agents/releases', baseUrl), { signal: controller.signal }),
          fetch(new URL('/api/v1/agents/upgrades', baseUrl), { signal: controller.signal }),
        ]);
        if (disposed) return;
        if (held.ok) {
          const answer = (await held.json()) as AgentReleases;
          setReleases((previous) => (sameReleases(previous, answer) ? previous : answer));
        }
        if (run.ok) {
          const answer = (await run.json()) as Rollout | null;
          setRollout((previous) => (sameRollout(previous, answer) ? previous : answer));
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
  }, [baseUrl, attentive, running, nonce]);

  const act = useCallback(
    async (engineId: string, verb: 'approve' | 'revoke') => {
      if (isMockMode()) {
        setBusy(engineId);
        setError(null);
        setAgents((previous) =>
          previous.map((agent) =>
            agent.engine_id === engineId
              ? { ...agent, status: verb === 'approve' ? 'approved' : 'revoked' }
              : agent,
          ),
        );
        setBusy(null);
        return;
      }
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
    if (isMockMode()) {
      return {
        token: 'mock-join-token-not-real',
        expires_at: Date.now() / 1000 + TOKEN_TTL_MINUTES * 60,
        ca_fingerprint: 'aa:bb:cc:dd:ee:ff',
        install:
          'curl -fsSL https://example.invalid/install-agent.sh | sudo sh -s -- --token mock-join-token-not-real',
        manual: 'sudo bystack-agent enroll --token mock-join-token-not-real',
      };
    }
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

  const upgrade = useCallback(
    async (version?: string): Promise<string | null> => {
      if (isMockMode()) return 'The demo has no fleet to upgrade.';
      setError(null);
      try {
        const response = await fetch(new URL('/api/v1/agents/upgrades', baseUrl), {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ version: version ?? '' }),
        });
        if (!response.ok) {
          // Returned rather than pushed into `error`, because every reason a
          // rollout cannot start is a sentence about the fleet — read-only, a
          // run already going, nothing signed to send — and it belongs beside
          // the button that was pressed rather than in the panel's error slot
          // with the failed approvals.
          return await detailOf(response, 'The Controller would not start a rollout.');
        }
        setRollout((await response.json()) as Rollout);
        setDismissed(0);
        return null;
      } catch {
        return 'Could not reach the Controller.';
      }
    },
    [baseUrl],
  );

  const stopUpgrade = useCallback(async () => {
    if (isMockMode()) return;
    try {
      const response = await fetch(new URL('/api/v1/agents/upgrades/cancel', baseUrl), {
        method: 'POST',
      });
      if (response.ok) setRollout((await response.json()) as Rollout);
    } catch {
      setError('Could not reach the Controller.');
    }
  }, [baseUrl]);

  // Dismissal is remembered by *when the run started*, not by a boolean. A
  // boolean would hide the next run too, and the next run is the one somebody
  // pressed the button for.
  const dismissRollout = useCallback(() => setDismissed(rollout?.started_at ?? 0), [rollout]);

  const dismissError = useCallback(() => setError(null), []);

  return {
    agents,
    loaded,
    unreachable,
    terms,
    releases,
    rollout: rollout && rollout.started_at === dismissed ? null : rollout,
    upgrade,
    stopUpgrade,
    dismissRollout,
    error,
    busy,
    approve,
    revoke,
    mint,
    dismissError,
  };
}

/**
 * Whether two release answers say the same thing.
 *
 * Same argument as `sameFleet`: this is polled every four seconds and a new
 * object identity each time would re-render the panel to report that nothing
 * changed. Compared on what is drawn — the versions held and the two host
 * counts — rather than field by field.
 */
function sameReleases(before: AgentReleases | null, after: AgentReleases): boolean {
  return (
    before !== null &&
    before.directory === after.directory &&
    before.releases.length === after.releases.length &&
    before.releases.every((release, index) => release.version === after.releases[index].version) &&
    before.upgradable.join() === after.upgradable.join() &&
    before.stale.join() === after.stale.join()
  );
}

/**
 * Whether two rollout answers say the same thing.
 *
 * `current` and the result count are the whole of what moves during a run, so
 * they are what is compared. `started_at` distinguishes one run from the next.
 */
function sameRollout(before: Rollout | null, after: Rollout | null): boolean {
  if (before === null || after === null) return before === after;
  return (
    before.started_at === after.started_at &&
    before.state === after.state &&
    before.current === after.current &&
    before.detail === after.detail &&
    before.results.length === after.results.length
  );
}

/** FastAPI's `{"detail": …}`, when there is one worth showing. */
async function detailOf(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === 'string' ? body.detail : `${fallback} (${response.status})`;
}
