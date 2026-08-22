/**
 * What a deployment run means on screen (ADR-0019).
 *
 * Pure, and separate from the hook that fetches it for the reason every other
 * `model/` file in this feature is: what a phase *looks like* is a decision
 * with cases in it, and cases belong somewhere a test can enumerate them.
 *
 * The one that is not cosmetic is `skipped`. A machine that already runs an
 * agent is a **correct** outcome for "make this machine managed", and drawing
 * it as a failure would send an operator to look at a host that is working.
 */

import type { DeployHost, DeployRun } from '../../../api/types';

/** How a row reads: what it is doing, and whether that is good news. */
export type Tone = 'pending' | 'busy' | 'good' | 'bad';

export interface RowView {
  readonly address: string;
  readonly label: string;
  readonly tone: Tone;
  readonly detail: string;
  readonly fingerprint: string;
}

const LABELS: Record<DeployHost['phase'], string> = {
  waiting: 'Waiting',
  connecting: 'Connecting',
  preparing: 'Looking at it',
  installing: 'Installing',
  enrolling: 'Waiting for it to connect',
  done: 'Connected',
  skipped: 'Already managed',
  failed: 'Failed',
};

const TONES: Record<DeployHost['phase'], Tone> = {
  waiting: 'pending',
  connecting: 'busy',
  preparing: 'busy',
  installing: 'busy',
  enrolling: 'busy',
  done: 'good',
  // Good, not bad. See the module comment — this is the one that matters.
  skipped: 'good',
  failed: 'bad',
};

export function rowView(host: DeployHost): RowView {
  return {
    address: host.port === 22 ? host.host : `${host.host}:${host.port}`,
    label: LABELS[host.phase],
    tone: TONES[host.phase],
    detail: host.detail,
    fingerprint: host.fingerprint,
  };
}

export interface RunSummary {
  /** One line under the list. Written to be read while it changes. */
  readonly headline: string;
  readonly tone: Tone;
  /** Whether the dialog should keep the form disabled. */
  readonly busy: boolean;
}

/**
 * The sentence under the list.
 *
 * A run stops at the first host that fails, so a finished run with a failure
 * is not "3 of 5 succeeded" — it is "it stopped here, and these were never
 * attempted". Reporting the untried ones as failures is the thing this is
 * written to avoid, because it sends somebody to check machines nothing has
 * touched.
 */
export function summarise(run: DeployRun | null): RunSummary {
  if (run === null) {
    return { headline: '', tone: 'pending', busy: false };
  }

  const settled = run.hosts.filter(
    (host) => host.phase === 'done' || host.phase === 'skipped',
  ).length;
  const failed = run.hosts.find((host) => host.phase === 'failed');
  const untried = run.hosts.filter((host) => host.phase === 'waiting').length;

  if (run.running) {
    const current = run.hosts.find(
      (host) => host.phase !== 'waiting' && host.phase !== 'done' && host.phase !== 'skipped',
    );
    return {
      headline: current
        ? `${rowView(current).label.toLowerCase()} on ${rowView(current).address}…`
        : 'Starting…',
      tone: 'busy',
      busy: true,
    };
  }

  if (failed) {
    const rest =
      untried > 0
        ? ` The run stopped there, so ${untried} host${untried === 1 ? '' : 's'} ${
            untried === 1 ? 'was' : 'were'
          } not attempted.`
        : '';
    return {
      headline: `${rowView(failed).address} failed.${rest}`,
      tone: 'bad',
      busy: false,
    };
  }

  return {
    headline:
      settled === 1
        ? '1 host is managed.'
        : `${settled} hosts are managed.`,
    tone: 'good',
    busy: false,
  };
}

/**
 * Whether the form can be submitted, and why not when it cannot.
 *
 * Checked here as well as on the Controller, and the two are not redundant:
 * this one exists so the button is disabled with a reason beside it, and that
 * one exists because a browser is not a place to enforce anything.
 */
export function submitReason(
  hosts: string,
  user: string,
  secret: string,
  updatable: boolean,
): string | null {
  if (!updatable) {
    return 'This Controller is read-only, so it will not install anything on another machine.';
  }
  if (hosts.trim() === '') return 'Add at least one address.';
  if (user.trim() === '') return 'Which account should it log in as?';
  if (secret.trim() === '') return 'A password or a private key is needed to log in.';
  return null;
}
