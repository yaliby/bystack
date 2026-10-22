/**
 * The session's lifecycle: find out on load, take a BYheaven ticket if one is
 * in the address, and keep asking so an expired or revoked session sends the
 * browser back to the sign-in instead of leaving a dashboard of 401s.
 */

import { useCallback, useEffect, useState } from 'react';
import { isMockMode } from '../../../mock/demoMode';
import {
  post,
  resolveSession,
  ssoTicket,
  withoutSso,
  type Outcome,
  type Session,
} from './session';

/** A revoked account should be out within about this long. */
const RECHECK_MS = 30_000;

export interface SessionControl {
  readonly session: Session;
  /** Resolves to the reason it failed, or `null` when it worked. */
  readonly signIn: (username: string, password: string) => Promise<string | null>;
  readonly setup: (username: string, password: string, token: string) => Promise<string | null>;
  readonly signOut: () => Promise<void>;
}

/**
 * Take the BYheaven ticket out of the address, if there is one, and spend it.
 *
 * The ticket is removed from the address bar before anything else happens, so
 * it is never in history or a screenshot, and it works once either way.
 */
function attemptFromAddress(baseUrl: string): Promise<Outcome> | null {
  const ticket = ssoTicket(window.location.hash);
  if (!ticket) return null;
  const { pathname, search, hash } = window.location;
  window.history.replaceState(null, '', pathname + search + withoutSso(hash));
  return post(baseUrl, 'sso', { ticket });
}

/**
 * The attempt made by the page load itself, shared by every mount.
 *
 * React's StrictMode runs effects twice in development: without this the
 * second run would find the ticket already gone from the address, and the
 * first run's answer would have been thrown away with its cleanup, leaving a
 * signed-in browser looking at a sign-in form.
 */
let loadAttempt: Promise<Outcome | null> | undefined;

function takeLoadAttempt(baseUrl: string): Promise<Outcome | null> {
  loadAttempt ??= Promise.resolve(attemptFromAddress(baseUrl));
  return loadAttempt;
}

export function useSession(baseUrl: string): SessionControl {
  const [session, setSession] = useState<Session>(
    isMockMode() ? { kind: 'open' } : { kind: 'checking' },
  );

  useEffect(() => {
    if (isMockMode()) return;
    let disposed = false;
    const apply = (next: Session) => {
      if (!disposed) setSession(next);
    };

    const settle = async (outcome: Outcome | null) => {
      if (outcome?.ok) return apply({ kind: 'signed-in', username: outcome.username });
      apply(
        await resolveSession(
          baseUrl,
          outcome ? `Single sign-on did not work: ${outcome.error}` : null,
        ),
      );
    };
    void takeLoadAttempt(baseUrl).then(settle);

    // BYheaven can point a tab that is already open at `#sso=…`, which changes
    // the fragment without loading the page again.
    const onHash = () => {
      const attempt = attemptFromAddress(baseUrl);
      if (attempt) void attempt.then(settle);
    };
    window.addEventListener('hashchange', onHash);

    // Only a *change* is applied: a new object every 30 s would re-render the
    // whole app, canvas included, to say nothing happened.
    const recheck = async () => {
      const next = await resolveSession(baseUrl, 'Your session ended. Sign in again.');
      if (next.kind === 'unreachable') return; // a blip is not a sign-out
      setSession((current) =>
        current.kind === 'signed-in' && next.kind === 'signed-in' && current.username === next.username
          ? current
          : next,
      );
    };
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void recheck();
    }, RECHECK_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void recheck();
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('hashchange', onHash);
    };
  }, [baseUrl]);

  const enter = useCallback(
    async (path: string, body: unknown): Promise<string | null> => {
      const outcome = await post(baseUrl, path, body);
      if (!outcome.ok) return outcome.error;
      setSession({ kind: 'signed-in', username: outcome.username });
      return null;
    },
    [baseUrl],
  );

  const signIn = useCallback(
    (username: string, password: string) => enter('login', { username, password }),
    [enter],
  );
  const setup = useCallback(
    (username: string, password: string, setup_token: string) =>
      enter('setup', { username, password, setup_token }),
    [enter],
  );
  const signOut = useCallback(async () => {
    await post(baseUrl, 'logout');
    setSession(await resolveSession(baseUrl));
  }, [baseUrl]);

  return { session, signIn, setup, signOut };
}
