/**
 * Operation presentation and confirmation rules.
 *
 * Pure: no React, no network. The interesting logic here is the consequence
 * of a backend decision — **a command does not change the graph** — and it is
 * the part most likely to be got wrong, so it lives somewhere it can be
 * tested without a browser.
 *
 * When a restart succeeds, the topology still shows `running` with the old
 * revision. It stays that way for a second or two, until the provider's watch
 * observes the container actually restarting and pushes a delta. A UI that
 * ignores this window looks broken ("I clicked restart and nothing happened").
 * A UI that closes it by patching its own graph is lying, and will keep lying
 * whenever the command succeeded but the change did not stick.
 *
 * So we do the third thing: say plainly that the command was applied and that
 * we are waiting for discovery to confirm it, and resolve that state from the
 * evidence — the target's content revision changing.
 */

import type {
  Actions,
  CommandKind,
  CommandResult,
  CommandStatus,
  GraphNode,
  RejectionReason,
  Urn,
} from '../../../api/types';

export const ACTION_LABEL: Record<CommandKind, string> = {
  start: 'Start',
  stop: 'Stop',
  restart: 'Restart',
  pause: 'Pause',
  unpause: 'Resume',
  kill: 'Kill',
};

/**
 * Actions that interrupt a workload without asking it first.
 *
 * `kill` sends SIGKILL — no grace period, no shutdown hook, no flush. `stop`
 * is graceful but still takes the service down. Both are styled apart and
 * confirmed; `restart` is not, because it is the one an operator reaches for
 * during an incident and a confirmation dialog in that moment is an obstacle,
 * not a safeguard.
 */
export const DESTRUCTIVE: ReadonlySet<CommandKind> = new Set<CommandKind>(['kill']);
export const DISRUPTIVE: ReadonlySet<CommandKind> = new Set<CommandKind>(['stop', 'kill', 'pause']);

/** Kinds whose nodes can be operated on at all. Everything else has no action bar. */
export const OPERABLE_KINDS: ReadonlySet<string> = new Set([
  'container',
  'service',
  'stack',
  'unit',
  'process',
]);

/**
 * Why the buttons are absent, in words an operator can act on.
 *
 * An unexplained empty action bar reads as a broken page — the user cannot
 * tell "you may not" from "this is still loading" from "we forgot to render
 * it". Each of these names the actual condition and, where there is one, the
 * fix.
 */
export const REASON_TEXT: Record<RejectionReason, string> = {
  read_only:
    'Read-only mode. Set read_only: false in bystack.yaml to enable operations.',
  unknown_target: 'This node is no longer in the graph.',
  unsupported_target: 'Nothing here can be operated on.',
  unsupported_state: 'Not available in this state.',
  provider_unavailable: 'The host is not connected right now.',
  too_many_targets: 'Too many containers for one operation — act on a service instead.',
};

export const STATUS_TEXT: Record<CommandStatus, string> = {
  in_flight: 'Working…',
  succeeded: 'Applied',
  noop: 'Already in that state',
  failed: 'Failed',
  timed_out: 'No answer yet — it may still be running',
  rejected: 'Refused',
};

/** Maps an outcome onto the inspector's existing status tones. */
export function toneOf(status: CommandStatus): 'good' | 'warning' | 'critical' | 'neutral' {
  switch (status) {
    case 'succeeded':
      return 'good';
    case 'noop':
      return 'neutral';
    case 'timed_out':
      return 'warning';
    case 'failed':
    case 'rejected':
      return 'critical';
    default:
      return 'neutral';
  }
}

/**
 * A completed command, plus what the graph looked like when it completed.
 *
 * The witness is the whole mechanism: the revisions we had at the moment the
 * engine said yes. Discovery has confirmed the change when those revisions
 * are no longer current.
 */
export interface Confirmation {
  readonly result: CommandResult;
  readonly witnessed: ReadonlyMap<Urn, string>;
  readonly since: number;
}

/** Capture the pre-change revisions of everything a command claims to have changed. */
export function witness(
  result: CommandResult,
  nodes: ReadonlyMap<Urn, GraphNode>,
  now: number,
): Confirmation {
  const witnessed = new Map<Urn, string>();
  for (const outcome of result.outcomes) {
    // Only targets that actually transitioned. A `noop` target was already in
    // the requested state, so its revision will never change and waiting on
    // it would leave the UI pending forever on a command that was, correctly,
    // reported as having done nothing.
    if (outcome.status !== 'succeeded') continue;
    const node = nodes.get(outcome.target);
    if (node) witnessed.set(outcome.target, node.revision);
  }
  return { result, witnessed, since: now };
}

/**
 * Has discovery caught up with what the command did?
 *
 * True when every witnessed target has either changed content or left the
 * graph. A container that was recreated rather than restarted disappears and
 * is replaced under a new id — which is confirmation, not a missing answer.
 */
export function confirmed(
  confirmation: Confirmation,
  nodes: ReadonlyMap<Urn, GraphNode>,
): boolean {
  if (confirmation.witnessed.size === 0) return true;
  for (const [urn, revision] of confirmation.witnessed) {
    const node = nodes.get(urn);
    if (node && node.revision === revision) return false;
  }
  return true;
}

/**
 * How long to keep waiting before saying so.
 *
 * A change normally lands within the provider's 250ms coalescing window plus
 * a round trip. Past this, something is wrong — the host dropped its event
 * stream, or the change was reverted — and continuing to show a spinner
 * would imply we still expect an answer we no longer expect.
 */
export const CONFIRM_TIMEOUT_MS = 12_000;

export type Phase =
  /** Dispatched; the engine has not answered. */
  | { readonly kind: 'running'; readonly command: CommandKind }
  /** The engine answered; discovery has not shown us the change yet. */
  | { readonly kind: 'confirming'; readonly result: CommandResult }
  /** Answered and observed. This is the only fully-resolved success. */
  | { readonly kind: 'done'; readonly result: CommandResult }
  /** Answered, but the graph never moved. Reported rather than hidden. */
  | { readonly kind: 'unconfirmed'; readonly result: CommandResult }
  | { readonly kind: 'error'; readonly message: string };

/** Resolve a completed command into a phase, given the current graph and clock. */
export function phaseOf(
  confirmation: Confirmation,
  nodes: ReadonlyMap<Urn, GraphNode>,
  now: number,
): Phase {
  if (!confirmation.result.outcomes.every((outcome) => isOk(outcome.status))) {
    // A failure needs no confirmation from discovery — the engine already
    // told us nothing changed, and the detail is what the operator needs.
    return { kind: 'done', result: confirmation.result };
  }
  if (confirmed(confirmation, nodes)) return { kind: 'done', result: confirmation.result };
  if (now - confirmation.since > CONFIRM_TIMEOUT_MS) {
    return { kind: 'unconfirmed', result: confirmation.result };
  }
  return { kind: 'confirming', result: confirmation.result };
}

function isOk(status: CommandStatus): boolean {
  return status === 'succeeded' || status === 'noop';
}

/**
 * One line describing a whole result.
 *
 * Names the failing targets when there are any, because "3 of 6 failed" is
 * unactionable and "shop-db-1: container is not running" is not.
 */
export function summarize(result: CommandResult, nameOf: (urn: Urn) => string): string {
  const failures = result.outcomes.filter((outcome) => !isOk(outcome.status));
  if (failures.length === 0) {
    const count = result.outcomes.length;
    const noun = count === 1 ? 'container' : 'containers';
    return `${STATUS_TEXT[result.status]} · ${count} ${noun}`;
  }
  return failures
    .map((outcome) => `${nameOf(outcome.target)}: ${outcome.detail ?? STATUS_TEXT[outcome.status]}`)
    .join(' · ');
}

/**
 * Whether a click needs confirming first.
 *
 * Scaled to blast radius, not just to the verb: killing one container is a
 * decision, killing a nine-service stack is a different decision, and the
 * count is the part an operator most often has not thought about.
 */
export function needsConfirmation(kind: CommandKind, actions: Actions): boolean {
  if (DESTRUCTIVE.has(kind)) return true;
  return DISRUPTIVE.has(kind) && actions.targets.length > 1;
}

export function confirmationText(
  kind: CommandKind,
  actions: Actions,
  name: string,
): string {
  const count = actions.targets.length;
  const scope = count === 1 ? name : `${name} (${count} containers)`;
  return `${ACTION_LABEL[kind]} ${scope}?`;
}
