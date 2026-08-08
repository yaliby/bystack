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

import type { ContainerLogs } from '../../../api/types';

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
  if (state.kind !== 'ready' || !state.logs.ok) return 0;
  return state.logs.lines.filter((line) => line.stderr).length;
}

/** Whether a re-read is worth offering: it is, unless one is already running. */
export function canRefresh(state: LogsState): boolean {
  return state.kind !== 'loading';
}
