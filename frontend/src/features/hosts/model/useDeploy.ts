/**
 * Starting a deployment and watching it (ADR-0019).
 *
 * Lifecycle and network only; what the answers *mean* is `deploy.ts`, which is
 * pure.
 *
 * **The credential goes out and is never held here.** It is an argument to
 * `start`, it reaches `fetch`, and this hook keeps no state that contains it —
 * no ref, no last-request memo, no retry that would need it again. A retry
 * asks the operator, which is the honest cost of not keeping the secret.
 */

import { useCallback, useEffect, useState } from 'react';
import type { DeployRun } from '../../../api/types';
import { isMockMode } from '../../../mock/demoMode';

/** While a run is going. Each host moves through several phases in a minute. */
const ACTIVE_INTERVAL_MS = 1_500;

export interface DeployCredential {
  readonly hosts: string;
  readonly user: string;
  readonly password: string;
  readonly privateKey: string;
  readonly passphrase: string;
}

export interface Deployment {
  /** `null` until this Controller has ever run one. */
  readonly run: DeployRun | null;
  /**
   * Begin. Returns the reason it could not, or `null` if it started.
   *
   * Returned rather than thrown, like `useController.update`: every reason is
   * a sentence about this Controller or that address, and it belongs beside
   * the button that was pressed.
   */
  readonly start: (credential: DeployCredential) => Promise<string | null>;
  /** Forget a finished run, so the dialog offers the form again. */
  readonly dismiss: () => void;
}

export function useDeploy(baseUrl: string): Deployment {
  const [run, setRun] = useState<DeployRun | null>(null);
  const [nonce, setNonce] = useState(0);

  const running = run?.running ?? false;

  useEffect(() => {
    if (isMockMode()) return;
    // Nothing to watch until a run has been started or one was already going
    // when this mounted. The first poll below establishes which.
    let disposed = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const response = await fetch(new URL('/api/v1/agents/deploy', baseUrl));
        if (!response.ok) throw new Error(`deploy ${response.status}`);
        const body = (await response.json()) as DeployRun | null;
        if (!disposed) setRun(body);
      } catch {
        // Left as it was. A blip mid-run is not news, and replacing a live
        // list of hosts with an error is how an operator loses the only view
        // they have of an install that is still going.
      }
      if (!disposed) {
        timer = window.setTimeout(() => void poll(), running ? ACTIVE_INTERVAL_MS : 15_000);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [baseUrl, running, nonce]);

  const start = useCallback(
    async (credential: DeployCredential): Promise<string | null> => {
      try {
        const response = await fetch(new URL('/api/v1/agents/deploy', baseUrl), {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            hosts: credential.hosts,
            user: credential.user,
            password: credential.password,
            private_key: credential.privateKey,
            passphrase: credential.passphrase,
          }),
        });
        if (response.status === 409) {
          const body = (await response.json()) as { detail?: string };
          return body.detail ?? 'This Controller will not start a deployment right now.';
        }
        if (!response.ok) return `The Controller answered ${response.status}.`;
        setRun((await response.json()) as DeployRun);
        setNonce((value) => value + 1);
        return null;
      } catch {
        return 'Could not reach the Controller.';
      }
    },
    [baseUrl],
  );

  const dismiss = useCallback(() => {
    setRun(null);
    setNonce((value) => value + 1);
  }, []);

  return { run, start, dismiss };
}
