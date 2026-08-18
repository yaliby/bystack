import { describe, expect, it } from 'vitest';
import type { AgentReleases, EnrolledAgent, Rollout } from '../../../api/types';
import { describeRollout, rolloutRows, upgradeOffer } from './upgrade';

const NOW_UNIX = 1_800_000_000;

function agent(engineId: string, version: string): EnrolledAgent {
  return {
    engine_id: engineId,
    status: 'approved',
    certificate_expires_at: NOW_UNIX + 90 * 24 * 3600,
    enrolled_at: NOW_UNIX - 3600,
    last_seen: NOW_UNIX - 60,
    agent_version: version,
    connected: true,
    local: false,
  };
}

function releases(upgradable: string[], version = '0.4.0'): AgentReleases {
  return {
    directory: '/var/lib/bystack/releases',
    releases: [
      { version, arch: 'x86_64', sha256: 'ab'.repeat(32), released_at: NOW_UNIX, size: 2_300_000 },
    ],
    upgradable,
    stale: [],
  };
}

function rollout(overrides: Partial<Rollout> = {}): Rollout {
  return {
    version: '0.4.0',
    state: 'running',
    planned: ['a', 'b', 'c'],
    current: 'a',
    detail: null,
    started_at: NOW_UNIX,
    finished_at: 0,
    results: [],
    ...overrides,
  };
}

describe('what the panel offers', () => {
  it('says nothing when the fleet is on the Controller’s version', () => {
    const fleet = [agent('a', '0.4.0'), agent('b', '0.4.0')];
    expect(upgradeOffer(fleet, '0.4.0', releases(['a', 'b']), null).kind).toBe('none');
  });

  it('offers the button only when a release exists and somebody can take it', () => {
    const fleet = [agent('a', '0.3.0')];
    // Held, and the host can verify it.
    expect(upgradeOffer(fleet, '0.4.0', releases(['a']), null)).toEqual({
      kind: 'push',
      version: '0.4.0',
      hosts: 1,
      installer: 0,
    });
    // Held, and nobody can verify it: a Controller with artifacts no agent
    // will accept cannot push, whatever the directory contains.
    expect(upgradeOffer(fleet, '0.4.0', releases([]), null).kind).toBe('installer');
    // Nothing held at all: the path every host used to get here.
    expect(upgradeOffer(fleet, '0.4.0', null, null).kind).toBe('installer');
  });

  it('counts the hosts that need the installer alongside the ones that do not', () => {
    // The mixed fleet this feature lands into and stays in: one host upgraded
    // since ADR-0017 and one from before it. Showing a single instruction here
    // leaves a machine behind with no explanation.
    const fleet = [agent('new', '0.3.0'), agent('old', '0.2.0')];
    expect(upgradeOffer(fleet, '0.4.0', releases(['new']), null)).toEqual({
      kind: 'push',
      version: '0.4.0',
      hosts: 1,
      installer: 1,
    });
  });

  it('lets a live run outrank the offer, and a finished one outrank silence', () => {
    const fleet = [agent('a', '0.3.0')];
    const live = upgradeOffer(fleet, '0.4.0', releases(['a']), rollout());
    expect(live.kind).toBe('running');

    // Nothing is behind any more, and the run that got them there is still
    // the most important thing on the screen.
    const done = upgradeOffer(
      [agent('a', '0.4.0')],
      '0.4.0',
      releases(['a']),
      rollout({ state: 'finished', current: null }),
    );
    expect(done.kind).toBe('ended');
  });
});

describe('describing a run', () => {
  it('names the host being touched and how many are confirmed', () => {
    const said = describeRollout(
      rollout({
        current: 'bbbbbbbbbbbbbbbbbb',
        results: [{ engine_id: 'a', state: 'confirmed', reason: null, version: '0.4.0' }],
      }),
    );
    expect(said).toContain('bbbbbbbbbbbb…');
    expect(said).toContain('1 of 3');
    // The property the whole design is for, said in the sentence an operator
    // reads while it is happening.
    expect(said).toContain('before the next one is touched');
  });

  it('carries the Controller’s own reason when a run stopped on a host', () => {
    const said = describeRollout(
      rollout({
        state: 'failed',
        current: null,
        detail: 'b: it staged 0.4.0 but has not come back on it within 120s.',
      }),
    );
    expect(said).toContain('has not come back');
  });

  it('does not describe a stopped run as a failure', () => {
    // Somebody pressed the button. The hosts not yet touched are fine, and
    // saying so is the difference between a report and an alarm.
    const said = describeRollout(rollout({ state: 'stopped', current: null }));
    expect(said).toContain('still on the version they were');
  });
});

describe('the per-host rows', () => {
  it('shows only the hosts a run reached, plus the one in flight', () => {
    // Never padded with the planned hosts it has not got to: a stopped run is
    // not going to touch them, and "pending" beside them would be a promise.
    const rows = rolloutRows(
      rollout({
        current: 'b',
        results: [{ engine_id: 'a', state: 'confirmed', reason: null, version: '0.4.0' }],
      }),
    );
    expect(rows.map((row) => row.state)).toEqual(['confirmed', 'in progress']);
  });

  it('keeps the reason a host gave', () => {
    const rows = rolloutRows(
      rollout({
        state: 'failed',
        current: null,
        results: [
          { engine_id: 'a', state: 'refused', reason: 'not signed by a key I hold', version: '' },
        ],
      }),
    );
    expect(rows[0].reason).toBe('not signed by a key I hold');
  });
});
