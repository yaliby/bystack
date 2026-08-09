/**
 * The three nothings.
 *
 * A container that has written nothing, a host whose agent is asleep, and a
 * Controller that did not answer all arrive as an empty list. What this file
 * pins is that they do not render alike — the failure is silent, looks
 * correct, and leads an operator to conclude their container is quiet when in
 * fact nobody asked it.
 */

import { describe, expect, it } from 'vitest';
import type { ContainerLogs } from '../../../api/types';
import {
  LIVE_BUFFER,
  LOGGABLE_KINDS,
  appendLive,
  canRefresh,
  isLive,
  noticeOf,
  stderrCount,
} from './logs';
import type { LogsState } from './logs';

const TARGET = 'bystack:container:e1/c1';

function answer(overrides: Partial<ContainerLogs> = {}): ContainerLogs {
  return { target: TARGET, ok: true, reason: null, lines: [], ...overrides };
}

describe('what has a log at all', () => {
  it('offers the panel on a container', () => {
    expect(LOGGABLE_KINDS.has('container')).toBe(true);
  });

  it('does not offer it on things that write nothing', () => {
    for (const kind of ['network', 'volume', 'image', 'host', 'stack', 'service']) {
      expect(LOGGABLE_KINDS.has(kind)).toBe(false);
    }
  });
});

describe('noticeOf', () => {
  it('says nothing at all before anything is asked', () => {
    expect(noticeOf({ kind: 'idle' })).toBeNull();
  });

  it('gets out of the way once there are lines to read', () => {
    const state = {
      kind: 'ready',
      logs: answer({ lines: [{ stderr: false, text: 'listening on :80' }] }),
    } as const;

    expect(noticeOf(state)).toBeNull();
  });

  it('calls a quiet container quiet, and does not call it broken', () => {
    const notice = noticeOf({ kind: 'ready', logs: answer() });

    expect(notice?.tone).toBe('muted');
    expect(notice?.text).toContain('written nothing');
  });

  it("passes on the Controller's reason verbatim when it could not ask", () => {
    // The wording belongs to whoever knew why. Paraphrasing here would lose
    // whether this is the engine refusing or the agent being absent.
    const notice = noticeOf({
      kind: 'ready',
      logs: answer({ ok: false, reason: 'the agent on lab-01 is not currently connected' }),
    });

    expect(notice?.tone).toBe('warning');
    expect(notice?.text).toBe('the agent on lab-01 is not currently connected');
  });

  it('still warns when a refusal arrives with no reason attached', () => {
    const notice = noticeOf({ kind: 'ready', logs: answer({ ok: false }) });

    expect(notice?.tone).toBe('warning');
    expect(notice?.text.length).toBeGreaterThan(0);
  });

  it('separates a browser that could not reach the Controller from a quiet container', () => {
    const unreachable = noticeOf({ kind: 'error', message: 'Could not reach the Controller.' });
    const quiet = noticeOf({ kind: 'ready', logs: answer() });

    expect(unreachable?.tone).toBe('warning');
    expect(quiet?.tone).toBe('muted');
    expect(unreachable?.text).not.toBe(quiet?.text);
  });
});

describe('stderrCount', () => {
  it('counts only the lines the container wrote to stderr', () => {
    const state = {
      kind: 'ready',
      logs: answer({
        lines: [
          { stderr: false, text: 'listening on :80' },
          { stderr: true, text: 'upstream timed out' },
          { stderr: true, text: 'exiting' },
        ],
      }),
    } as const;

    expect(stderrCount(state)).toBe(2);
  });

  it('claims nothing about a read that failed', () => {
    expect(stderrCount({ kind: 'ready', logs: answer({ ok: false, reason: 'offline' }) })).toBe(0);
    expect(stderrCount({ kind: 'error', message: 'offline' })).toBe(0);
    expect(stderrCount({ kind: 'idle' })).toBe(0);
  });
});

describe('canRefresh', () => {
  it('does not offer a second read while one is running', () => {
    expect(canRefresh({ kind: 'loading' })).toBe(false);
  });

  it('offers one after a failure, which is the case it is most wanted in', () => {
    expect(canRefresh({ kind: 'error', message: 'offline' })).toBe(true);
    expect(canRefresh({ kind: 'ready', logs: answer({ ok: false, reason: 'offline' }) })).toBe(true);
  });
});


// ---------------------------------------------------------------------------
// The live tail
// ---------------------------------------------------------------------------

function live(overrides: Partial<Extract<LogsState, { kind: 'live' }>> = {}): LogsState {
  return { kind: 'live', lines: [], dropped: 0, ended: null, ...overrides };
}

describe('a live tail', () => {
  it('says it is waiting rather than that the container is silent', () => {
    // The whole reason `live` is a separate state. An open stream with no
    // lines yet and a finished read with no lines are the same empty array
    // and opposite facts: one is "nothing has happened *yet*", the other is
    // "nothing happened". Rendering them alike tells an operator their
    // container is quiet when it has been watched for half a second.
    expect(noticeOf(live())).toEqual({ tone: 'muted', text: 'Waiting for output…' });
    expect(noticeOf(live({ ended: { reason: null } }))).toEqual({
      tone: 'muted',
      text: 'This container wrote nothing.',
    });
  });

  it('shows why a stream stopped, when it stopped for a reason', () => {
    // A container that exited cleanly and an agent that lost its daemon both
    // end the stream. Only one of them is the operator's problem.
    expect(noticeOf(live({ ended: { reason: 'the host disconnected' } }))).toEqual({
      tone: 'warning',
      text: 'the host disconnected',
    });
  });

  it('gets out of the way once there are lines to read', () => {
    expect(noticeOf(live({ lines: [{ stderr: false, text: 'up' }] }))).toBeNull();
  });

  it('counts stderr the same way a one-shot read does', () => {
    expect(
      stderrCount(
        live({
          lines: [
            { stderr: false, text: 'listening' },
            { stderr: true, text: 'panic' },
          ],
        }),
      ),
    ).toBe(1);
  });

  it('offers no Refresh while it is following, and one once it is not', () => {
    // Refresh means "get the newest lines", which is what an open stream is
    // already doing. Offering it suggests the panel is stale when it is the
    // opposite.
    expect(canRefresh(live())).toBe(false);
    expect(canRefresh(live({ ended: { reason: null } }))).toBe(true);
  });

  it('reports whether anyone is still listening', () => {
    expect(isLive(live())).toBe(true);
    expect(isLive(live({ ended: { reason: null } }))).toBe(false);
    expect(isLive({ kind: 'idle' })).toBe(false);
  });
});

describe('appendLive', () => {
  it('keeps the newest lines when a container outruns the panel', () => {
    // A live log is unbounded and the DOM is not. The container that most
    // needs watching is the one writing fastest, so this bound is load-bearing
    // rather than defensive — and it is the case no manual test reaches.
    const previous = Array.from({ length: LIVE_BUFFER }, (_, i) => ({
      stderr: false,
      text: `old ${i}`,
    }));

    const result = appendLive(previous, [{ stderr: true, text: 'newest' }]);

    expect(result).toHaveLength(LIVE_BUFFER);
    expect(result[result.length - 1]).toEqual({ stderr: true, text: 'newest' });
    expect(result[0].text).toBe('old 1');
  });

  it('appends in order below the bound', () => {
    const result = appendLive([{ stderr: false, text: 'first' }], [{ stderr: false, text: 'second' }]);
    expect(result.map((l) => l.text)).toEqual(['first', 'second']);
  });
});
