/**
 * What the panel is allowed to say about updating the Controller itself.
 *
 * Pure: no React, no network. Same shape as `upgrade.ts` beside it and for the
 * same reason — the cases want different paragraphs, and a component assembled
 * from booleans reliably renders one that reads as nonsense.
 *
 * The thing this file exists to get right is that **an update is not a
 * request/response**. Somewhere in the middle of a successful one, the process
 * serving the dashboard is stopped, replaced and started again. Every fetch
 * during that window fails, and none of those failures is an error worth
 * showing: they are the operation working. So "the Controller is unreachable"
 * has two meanings here, and only one of them is a problem.
 */

import type { ControllerSelf, ControllerUpdate } from '../../../api/types';

/** What to draw where the "update this Controller" button lives. */
export type ControllerOffer =
  /** No local updater. The install path's own upgrade instructions apply. */
  | { readonly kind: 'unavailable'; readonly reason: string }
  /** Nothing has been asked for. Offer it. */
  | { readonly kind: 'idle'; readonly version: string }
  /** A run is under way. Watch it, and expect the connection to drop. */
  | { readonly kind: 'running'; readonly update: ControllerUpdate }
  /** A run ended. Worth reporting until it is dismissed. */
  | { readonly kind: 'ended'; readonly update: ControllerUpdate };

export function controllerOffer(self: ControllerSelf | null): ControllerOffer | null {
  // `null` and not a case: until the first answer arrives the panel makes no
  // claim. Offering a button before knowing whether there is an updater is how
  // an operator gets told to press something that cannot work.
  if (self === null) return null;
  if (!self.updatable) return { kind: 'unavailable', reason: self.reason };
  if (self.update === null) return { kind: 'idle', version: self.version };
  return self.update.running
    ? { kind: 'running', update: self.update }
    : { kind: 'ended', update: self.update };
}

/**
 * Roughly how far along a run is, for a bar that has to move.
 *
 * A number the *panel* invents, and it says so. The Controller deliberately
 * publishes no percentage: what it knows is which of six named steps root is
 * on. Turning that into a fraction is a presentation decision, and putting it
 * here rather than in the API is what stops it becoming a second model of the
 * update that can disagree with the first.
 *
 * Weighted by how long each step actually takes rather than spread evenly.
 * `fetching` is tens of megabytes and `probation` is up to three minutes;
 * `applying` is a `rename`. A bar that gave them equal thirds would sit at 33%
 * for the whole download and then jump.
 */
export function progressOf(update: ControllerUpdate): number {
  switch (update.phase) {
    case 'fetching':
      return 0.25;
    case 'verifying':
      return 0.45;
    case 'applying':
      return 0.55;
    case 'probation':
      return 0.8;
    case 'cascading':
      return 0.95;
    default:
      return 1;
  }
}

/**
 * One sentence about a run, written to be read while it is happening.
 *
 * The manager's own `detail` is preferred wherever it wrote one: it is the
 * process that actually did the thing, it has seen the error, and a summary
 * composed here would bury the part that says what to do. This supplies the
 * sentence for the phases where there is nothing to add.
 */
export function describeUpdate(update: ControllerUpdate): string {
  if (update.detail) return update.detail;
  switch (update.phase) {
    case 'fetching':
      return 'Downloading the release.';
    case 'verifying':
      return 'Checking the signature.';
    case 'applying':
      return 'Stopping the Controller and swapping it. This page will go quiet for a moment.';
    case 'probation':
      return 'Waiting for the new Controller to answer. If it does not, the previous one comes back on its own.';
    case 'cascading':
      return 'The Controller is up. Fetching the fleet’s agents.';
    case 'success':
      return `This Controller is ${update.version}.`;
    case 'rolled_back':
      return `The update did not come up, so the previous Controller was put back. This machine is on ${update.version}.`;
    default:
      return 'The update did not finish.';
  }
}

/**
 * Whether an operator has to look at how this ended.
 *
 * `rolled_back` counts, and it is the reason this is not just
 * `phase === 'failed'`. A rollback is the *system working* — but it means the
 * release that was published does not run here, which is a thing somebody
 * needs to know before they press the button again.
 */
export function updateFailed(update: ControllerUpdate): boolean {
  return update.phase === 'failed' || update.phase === 'rolled_back';
}

/**
 * Whether the Controller being unreachable right now is expected.
 *
 * True from the moment root is asked until the moment it reports a terminal
 * phase. During that window a failed fetch is the update working, and the
 * panel says "swapping" rather than "cannot reach the Controller" — which is
 * the difference between a progress bar and an operator opening an ssh
 * session.
 */
export function expectSilence(offer: ControllerOffer | null): boolean {
  return offer?.kind === 'running';
}
