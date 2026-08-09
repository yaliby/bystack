/**
 * What a log read means, as a pure function of the answer.
 *
 * The whole of the decision-making for the panel lives here so it can be
 * tested without a network, a component or a DOM — the same split
 * `operations.ts` uses, and for the same reason: the interesting cases are
 * the empty ones, and they are the ones a screenshot cannot tell apart.
 *
 * Three different nothings arrive at this panel and they must not render
 * alike: a container that has written nothing, a host whose agent is asleep,
 * and a Controller that did not answer. The first is a fact about the
 * workload, the second is a fact about the fleet, and the third is a fact
 * about this browser's connection.
 */

import type { ContainerLogs, LogLine } from '../../../api/types';

/**
 * Lines a live tail keeps in the browser.
 *
 * A live log is unbounded by nature and the DOM is not. Oldest-first
 * discarding, matching what the Controller does when *it* falls behind
 * (`STREAM_QUEUE`): somebody watching output scroll wants the newest lines,
 * and a panel that froze to preserve history nobody asked for would be the
 * wrong feature at every level of the stack.
 */
export const LIVE_BUFFER = 2000;

/**
 * Kinds that have a log at all.
 *
 * A structural fact about the model, not a policy: a network does not write
 * output. Policy — whether *this* host can be asked right now — stays on the
 * Controller and arrives in `reason`.
 */
export const LOGGABLE_KINDS: ReadonlySet<string> = new Set(['container']);

export type LogsState =
  /** Never asked. The panel is closed, or the selection just changed. */
  | { readonly kind: 'idle' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly logs: ContainerLogs }
  /**
   * A live tail. `ended` is `null` while it is still running.
   *
   * Distinct from `ready` rather than folded into it, because the two differ
   * in what an empty list *means*: `ready` with no lines is a container that
   * has written nothing and never will while we are looking, and `live` with
   * no lines is one that has not written anything **yet**. Rendering those
   * alike would tell an operator their container is silent when the truth is
   * that we have been watching it for half a second.
   */
  | {
      readonly kind: 'live';
      readonly lines: readonly LogLine[];
      readonly dropped: number;
      readonly ended: { readonly reason: string | null } | null;
    }
  /** The request itself failed — a 404, a 400, or an unreachable Controller. */
  | { readonly kind: 'error'; readonly message: string };

export type NoticeTone = 'muted' | 'warning';

export interface Notice {
  readonly tone: NoticeTone;
  readonly text: string;
}

/**
 * The line to show instead of output, or `null` when there are lines to show.
 *
 * `muted` for an answer, `warning` for a failure to get one. That distinction
 * is the entire job of this function: rendering "no output yet" and "this host
 * is offline" in the same grey text is how an operator concludes their
 * container is quiet when in fact nobody asked it.
 */
export function noticeOf(state: LogsState): Notice | null {
  switch (state.kind) {
    case 'idle':
      return null;
    case 'loading':
      return { tone: 'muted', text: 'Reading…' };
    case 'error':
      return { tone: 'warning', text: state.message };
    case 'live':
      if (state.ended?.reason) {
        // The stream stopped for a reason: the agent lost the daemon, the
        // host went away. Not the same as a container that exited quietly.
        return { tone: 'warning', text: state.ended.reason };
      }
      if (state.lines.length === 0) {
        return state.ended
          ? { tone: 'muted', text: 'This container wrote nothing.' }
          : // Present tense on purpose. The stream is open and the answer may
            // still change, which is the whole difference from a one-shot read.
            { tone: 'muted', text: 'Waiting for output…' };
      }
      return null;
    case 'ready':
      if (!state.logs.ok) {
        // The Controller's own words, verbatim. It has the engine's message
        // and the reason the agent could not be asked, and paraphrasing here
        // would lose which of the two this is.
        return { tone: 'warning', text: state.logs.reason ?? 'The log could not be read.' };
      }
      if (state.logs.lines.length === 0) {
        return { tone: 'muted', text: 'This container has written nothing.' };
      }
      return null;
  }
}

/** How many of the shown lines came from stderr. Zero is worth saying nothing about. */
export function stderrCount(state: LogsState): number {
  if (state.kind === 'live') return state.lines.filter((line) => line.stderr).length;
  if (state.kind !== 'ready' || !state.logs.ok) return 0;
  return state.logs.lines.filter((line) => line.stderr).length;
}

/** The lines to render, whichever way they were fetched. */
export function linesOf(state: LogsState): readonly LogLine[] {
  if (state.kind === 'live') return state.lines;
  if (state.kind === 'ready' && state.logs.ok) return state.logs.lines;
  return [];
}

/**
 * Whether the panel is watching a stream that is still open.
 *
 * Drives the "Live" indicator. An operator looking at a still panel needs to
 * know whether nothing is happening or nothing is *listening*.
 */
export function isLive(state: LogsState): boolean {
  return state.kind === 'live' && state.ended === null;
}

/**
 * Append a chunk to a live tail, keeping the buffer bounded.
 *
 * Pure, so the bound is testable without a stream: the case that matters is
 * the container writing faster than anyone can read, and that is exactly the
 * one a manual test never reaches.
 */
export function appendLive(
  previous: readonly LogLine[],
  incoming: readonly LogLine[],
): readonly LogLine[] {
  const combined = previous.concat(incoming);
  return combined.length > LIVE_BUFFER ? combined.slice(combined.length - LIVE_BUFFER) : combined;
}

/** Whether a re-read is worth offering: it is, unless one is already running. */
export function canRefresh(state: LogsState): boolean {
  // A live tail has nothing to refresh *to* while it is open — the newest
  // lines are already arriving. Offering the button would suggest the panel
  // is stale when it is the opposite of stale.
  if (isLive(state)) return false;
  return state.kind !== 'loading';
}
