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
import { LOGGABLE_KINDS, canRefresh, noticeOf, stderrCount } from './logs';

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
