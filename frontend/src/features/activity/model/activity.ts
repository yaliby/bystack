/**
 * What the operations timeline is allowed to say.
 *
 * Pure: no React, no network. The panel renders these answers and decides
 * nothing itself.
 *
 * The log this reads is the Controller's audit ring (`infra/audit/memory.py`),
 * and two of its properties drive most of what is here.
 *
 * **It records refusals.** A read-only control plane that declined to restart
 * a dead service is the single most useful line the log holds, and a timeline
 * that showed only what succeeded would omit exactly the entry someone is
 * looking for. So `rejected` is a first-class outcome with its own words, not
 * an error state.
 *
 * **It is in memory and every actor is `anonymous`** (ADR-0012). Both are
 * stated in the panel rather than papered over: a timeline that looked durable
 * would be trusted as an audit log, and it is not one until authentication and
 * durable storage arrive — which is the same reason no destructive operation
 * exists yet.
 */

import type { AuditEntry, CommandStatus, Urn } from '../../../api/types';

/**
 * How an entry reads at a glance.
 *
 * `noop` is muted rather than green on purpose. "The container was already
 * stopped" is not a change, and colouring it as success would have an operator
 * believe their click did something.
 */
export type Tone = 'ok' | 'muted' | 'warn' | 'bad';

const TONES: Record<CommandStatus, Tone> = {
  succeeded: 'ok',
  noop: 'muted',
  in_flight: 'muted',
  failed: 'bad',
  timed_out: 'warn',
  rejected: 'warn',
};

export function toneOf(status: CommandStatus): Tone {
  return TONES[status] ?? 'muted';
}

/** The word on the chip. Short enough for a narrow panel, plain enough to scan. */
const STATUS_WORD: Record<CommandStatus, string> = {
  succeeded: 'Applied',
  noop: 'No change',
  in_flight: 'In flight',
  failed: 'Failed',
  timed_out: 'Timed out',
  rejected: 'Refused',
};

export function statusWord(status: CommandStatus): string {
  return STATUS_WORD[status] ?? status;
}

/**
 * One sentence about what happened, in the terms the operator asked in.
 *
 * The Controller's own `detail` wins where it gave one — it is the engine's
 * answer or the reason for a refusal, and rewording it here would lose the
 * only thing on the row that came from outside this browser.
 */
export function describeEntry(entry: AuditEntry, fanOut: number): string {
  if (entry.detail) return entry.detail;
  switch (entry.status) {
    case 'succeeded':
      return fanOut > 1
        ? `Applied to ${fanOut} containers.`
        : 'Applied. The graph changes when discovery observes it.';
    case 'noop':
      return 'Already in that state. Nothing was changed.';
    case 'in_flight':
      return 'Sent, and not yet answered.';
    case 'timed_out':
      return 'The host did not answer in time. It may still be completing.';
    case 'rejected':
      return 'Refused before dispatch. The host was never contacted.';
    case 'failed':
      return 'The host refused or could not complete it.';
  }
}

/**
 * A logical target fans out to the containers realizing it (ARCHITECTURE §9),
 * and the count is worth showing because it is the difference between
 * restarting one container and restarting six.
 */
export function fanOutOf(entry: AuditEntry): number {
  return entry.targets.length;
}

/**
 * Whether the timeline can take the operator to what an entry is about.
 *
 * The map is the control surface, so a row is a way back to the node rather
 * than a line of text about it. It stops being one when the node is gone —
 * a container recreated by `compose up` has a new id, and the entry names the
 * one that was killed. Saying so is better than a click that does nothing.
 */
export function isReachable(target: Urn, nodes: ReadonlyMap<Urn, unknown>): boolean {
  return nodes.has(target);
}

/** Coarse and past-tense, matching the hosts panel: "4m ago". */
export function describeAge(atUnixSeconds: number, now: number): string {
  const elapsed = now - atUnixSeconds * 1000;
  if (elapsed < 0 || elapsed < 5_000) return 'just now';
  const seconds = Math.floor(elapsed / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/**
 * How long it took, when that is worth knowing.
 *
 * Under a second is the ordinary case and printing "0s" on every row is noise;
 * a slow one is a fact about the host and is worth the space.
 */
export function describeDuration(ms: number): string | null {
  if (ms < 1_000) return null;
  return ms < 10_000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms / 1000)}s`;
}

/**
 * Whether two answers from `GET /commands/audit` say the same thing.
 *
 * Same reason as `sameFleet`: publishing a new array identity every few
 * seconds would re-render the panel to report that nothing happened. Ids are
 * enough — an entry is written once and its status is final by the time it is
 * in the ring, apart from `in_flight`, which is why the status is compared too.
 */
export function sameActivity(
  before: readonly AuditEntry[],
  after: readonly AuditEntry[],
): boolean {
  if (before.length !== after.length) return false;
  return before.every(
    (entry, index) => entry.id === after[index].id && entry.status === after[index].status,
  );
}
