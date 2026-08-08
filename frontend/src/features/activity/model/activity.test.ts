import { describe, expect, it } from 'vitest';
import type { AuditEntry, CommandStatus, Urn } from '../../../api/types';
import {
  describeAge,
  describeDuration,
  describeEntry,
  fanOutOf,
  isReachable,
  sameActivity,
  statusWord,
  toneOf,
} from './activity';

const NOW = 1_800_000_000_000;
const NOW_UNIX = NOW / 1000;

const CONTAINER = 'bystack:container:ENGINE/abc' as Urn;
const SERVICE = 'bystack:service:ENGINE/shop/web' as Urn;

function entry(overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id: 'op-1',
    at: NOW_UNIX - 30,
    actor: 'anonymous',
    kind: 'restart',
    target: CONTAINER,
    targets: [CONTAINER],
    status: 'succeeded' as CommandStatus,
    detail: null,
    reason: null,
    duration_ms: 240,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// what the outcomes mean
// ---------------------------------------------------------------------------

describe('outcomes that are not failures are not coloured as failures', () => {
  it('mutes a no-op rather than reporting it as success', () => {
    // "The container was already stopped" is not a change. Green would have an
    // operator believe their click did something.
    expect(toneOf('noop')).toBe('muted');
    expect(statusWord('noop')).toBe('No change');
  });

  it('keeps a refusal visible without calling it an error', () => {
    // A read-only Controller declining to restart a dead service is the single
    // most useful line the log holds, and it is not a fault.
    expect(toneOf('rejected')).toBe('warn');
    expect(statusWord('rejected')).toBe('Refused');
  });

  it('distinguishes a timeout from a failure', () => {
    // A timed-out command may still be completing on the host; a failed one
    // was answered. Collapsing them sends the operator to the wrong place.
    expect(toneOf('timed_out')).toBe('warn');
    expect(toneOf('failed')).toBe('bad');
  });
});

describe('the Controller’s own words win', () => {
  it('shows the detail it gave rather than a generic sentence', () => {
    // It is the engine's answer or the reason for a refusal, and the only
    // thing on the row that came from outside this browser.
    const said = describeEntry(entry({ status: 'failed', detail: 'No such container: abc' }), 1);
    expect(said).toBe('No such container: abc');
  });

  it('explains a successful fan-out by how far it fanned', () => {
    const said = describeEntry(entry({ target: SERVICE, targets: [CONTAINER, SERVICE] }), 2);
    expect(said).toContain('2 containers');
  });

  it('says what a bare success does and does not mean', () => {
    // The graph changes when discovery observes the transition, not when the
    // command returns (ARCHITECTURE section 9).
    expect(describeEntry(entry(), 1)).toContain('discovery');
  });
});

describe('fan-out', () => {
  it('counts the containers a logical target expanded to', () => {
    expect(fanOutOf(entry({ targets: [CONTAINER, SERVICE] }))).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// a row is a way back to the node
// ---------------------------------------------------------------------------

describe('reaching the node an entry is about', () => {
  it('is reachable while the node is on the canvas', () => {
    expect(isReachable(CONTAINER, new Map([[CONTAINER, {}]]))).toBe(true);
  });

  it('is not, once the container has been recreated under a new id', () => {
    // `compose up` gives it a new id, and the entry names the one that was
    // killed. A click that silently did nothing would read as a broken panel.
    expect(isReachable(CONTAINER, new Map())).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// time
// ---------------------------------------------------------------------------

describe('ages and durations', () => {
  it('reads as past tense at every scale', () => {
    expect(describeAge(NOW_UNIX - 2, NOW)).toBe('just now');
    expect(describeAge(NOW_UNIX - 30, NOW)).toBe('30s ago');
    expect(describeAge(NOW_UNIX - 300, NOW)).toBe('5m ago');
    expect(describeAge(NOW_UNIX - 7200, NOW)).toBe('2h ago');
    expect(describeAge(NOW_UNIX - 5 * 86400, NOW)).toBe('5d ago');
  });

  it('says nothing about a duration under a second', () => {
    // The ordinary case. "0s" on every row is noise, and a slow operation is
    // a fact about the host worth the space.
    expect(describeDuration(240)).toBeNull();
    expect(describeDuration(2_400)).toBe('2.4s');
    expect(describeDuration(45_000)).toBe('45s');
  });
});

// ---------------------------------------------------------------------------
// not re-rendering to say nothing happened
// ---------------------------------------------------------------------------

describe('change detection', () => {
  it('treats an unchanged log as unchanged', () => {
    expect(sameActivity([entry()], [entry()])).toBe(true);
  });

  it('notices a new operation', () => {
    expect(sameActivity([entry()], [entry({ id: 'op-2' }), entry()])).toBe(false);
  });

  it('notices an in-flight operation finishing', () => {
    // The one field that moves after an entry is written.
    expect(sameActivity([entry({ status: 'in_flight' })], [entry()])).toBe(false);
  });
});
