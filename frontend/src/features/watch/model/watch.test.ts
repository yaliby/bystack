import { describe, expect, it } from 'vitest';
import type {
  EnrolledAgent,
  GraphNode,
  Inventory,
  InventoryItem,
  Urn,
  WatchEntry,
  WatchFanout,
} from '../../../api/types';
import {
  draftFrom,
  entrySubtitle,
  entryTitle,
  fanoutCandidates,
  fanoutSummary,
  groupNote,
  inventoryNotice,
  stateLabel,
  watchRows,
} from './watch';

function entry(overrides: Partial<WatchEntry> = {}): WatchEntry {
  return {
    id: 'w1',
    engine_id: 'ENGINE',
    kind: 'unit',
    group_id: 'g1',
    group_hosts: 1,
    name: 'nginx.service',
    match_kind: null,
    pattern: '',
    label: '',
    added_at: 0,
    urn: 'bystack:unit:ENGINE/nginx.service',
    ...overrides,
  };
}

function node(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    urn: 'bystack:unit:ENGINE/nginx.service',
    kind: 'unit',
    name: 'nginx.service',
    source: 'ENGINE',
    status: 'active',
    labels: {},
    attrs: {},
    observed_at: 0,
    revision: 'r1',
    ...overrides,
  };
}

function item(overrides: Partial<InventoryItem> = {}): InventoryItem {
  return { id: 'nginx.service', name: 'nginx.service', description: '', state: '', detail: '', pid: 0, ...overrides };
}

describe('a stored entry and a drawn card are different things', () => {
  it('is pending only until the host has said something', () => {
    const nodes = new Map<Urn, GraphNode>();
    expect(watchRows([entry()], nodes)[0].pending).toBe(true);
    expect(watchRows([entry()], nodes)[0].state).toBe('Waiting for this host');

    nodes.set(node().urn, node());
    expect(watchRows([entry()], nodes)[0].pending).toBe(false);
  });

  it('does not call a unit that is not installed "pending"', () => {
    // The host answered. The answer was that the unit does not exist, which
    // is a state and not a silence -- and the difference decides whether an
    // operator waits or goes and installs something.
    const nodes = new Map<Urn, GraphNode>([[node().urn, node({ status: 'not-found' })]]);
    const row = watchRows([entry()], nodes)[0];
    expect(row.pending).toBe(false);
    expect(row.state).toBe('Not installed');
  });

  it('says what the two most misread states actually mean', () => {
    expect(stateLabel(node({ status: 'absent' }))).toBe('Not running');
    expect(stateLabel(node({ status: 'inactive' }))).toBe('Stopped');
    expect(stateLabel(node({ status: 'error' }))).toBe('Cannot be read');
    // Unknown states are shown rather than hidden: a newer systemd inventing
    // one must degrade to a word nobody recognises, never to a blank.
    expect(stateLabel(node({ status: 'reloading' }))).toBe('reloading');
  });
});

describe('what an entry is called', () => {
  it('prefers the operator’s own words', () => {
    expect(entryTitle(entry({ label: 'The web server' }))).toBe('The web server');
  });

  it('falls back to the thing itself rather than to an id', () => {
    expect(entryTitle(entry())).toBe('nginx.service');
    expect(
      entryTitle(entry({ kind: 'process', name: '', match_kind: 'exec', pattern: '/usr/local/bin/mydaemon' })),
    ).toBe('mydaemon');
    expect(
      entryTitle(entry({ kind: 'process', name: '', match_kind: 'cmdline', pattern: 'worker.py' })),
    ).toBe('worker.py');
  });

  it('says how a rule matches, because two rules can name the same word', () => {
    expect(entrySubtitle(entry())).toBe('systemd unit');
    expect(entrySubtitle(entry({ kind: 'process', match_kind: 'cmdline', pattern: 'worker.py' }))).toBe(
      'command line contains worker.py',
    );
    expect(entrySubtitle(entry({ kind: 'process', match_kind: 'name', pattern: 'python3' }))).toBe(
      'named python3',
    );
  });
});

describe('picking a row', () => {
  it('watches a unit by its own name', () => {
    expect(draftFrom('unit', item())).toEqual({
      kind: 'unit',
      name: 'nginx.service',
      match_kind: null,
      pattern: '',
    });
  });

  it('prefers the executable path, because it is the precise choice', () => {
    expect(draftFrom('process', item({ id: '/usr/local/bin/mydaemon' }))).toEqual({
      kind: 'process',
      name: '',
      match_kind: 'exec',
      pattern: '/usr/local/bin/mydaemon',
    });
  });

  it('falls back to the name for a process this agent cannot resolve', () => {
    // `/proc/<pid>/exe` is unreadable for anything the agent does not own, so
    // the picker offers `comm` -- and a rule built from it has to say `name`,
    // or it would be an exec rule that matches nothing forever.
    expect(draftFrom('process', item({ id: 'postgres' })).match_kind).toBe('name');
  });
});

describe('what the picker says above its rows', () => {
  const base: Inventory = { engine_id: 'E', kind: 'unit', ok: true, reason: null, items: [], total: 0 };

  it('is silent when the list speaks for itself', () => {
    expect(inventoryNotice(null)).toBeNull();
    expect(inventoryNotice({ ...base, items: [item()], total: 1 })).toBeNull();
  });

  it('names a refusal rather than showing it as an empty machine', () => {
    expect(inventoryNotice({ ...base, ok: false, reason: 'the agent is not connected' })).toBe(
      'the agent is not connected',
    );
  });

  it('says a list is truncated, because a short list reads as the whole truth', () => {
    expect(inventoryNotice({ ...base, items: [item(), item()], total: 412 })).toContain('2 of 412');
  });
});

describe('one selection made on several hosts', () => {
  function agent(overrides: Partial<EnrolledAgent> = {}): EnrolledAgent {
    return {
      engine_id: 'E1',
      status: 'approved',
      certificate_expires_at: 0,
      enrolled_at: 0,
      last_seen: 0,
      agent_version: '0.2.0',
      connected: true,
      local: false,
      ...overrides,
    };
  }

  it('says nothing on a row that was only ever chosen here', () => {
    // The ordinary case, and a note on every row is a note nobody reads.
    expect(groupNote(entry({ group_hosts: 1 }))).toBeNull();
  });

  it('counts the other hosts and never this one', () => {
    // Three hosts hold the group; the row is on one of them, so two are news.
    expect(groupNote(entry({ group_hosts: 3 }))).toBe('also chosen on 2 other hosts');
    expect(groupNote(entry({ group_hosts: 2 }))).toBe('also chosen on 1 other host');
  });

  it('offers no host whose agent would never be told', () => {
    // Pending has no agent that will ever hear the list, and revoked has one
    // we have decided not to listen to. Intent stored for either is durable,
    // correct, and permanently unobserved.
    const candidates = fanoutCandidates(
      [
        agent({ engine_id: 'HERE' }),
        agent({ engine_id: 'PENDING', status: 'pending' }),
        agent({ engine_id: 'GONE', status: 'revoked' }),
        agent({ engine_id: 'OTHER' }),
        agent({ engine_id: 'MINE', status: 'local' }),
      ],
      'HERE',
      () => null,
    );
    expect(candidates.map((host) => host.engineId)).toEqual(['MINE', 'OTHER']);
  });

  it('leaves out the host being edited, which is not optional', () => {
    expect(fanoutCandidates([agent({ engine_id: 'HERE' })], 'HERE', () => null)).toHaveLength(0);
  });

  it('names the hosts that need looking at rather than counting them', () => {
    // "Stored on 2 of 3" sends an operator to find the third. This knows
    // which one it is, and what it said.
    const result: WatchFanout = {
      group_id: 'g9',
      stored: 2,
      hosts: [
        { engine_id: 'A', stored: true, delivered: true, detail: null },
        { engine_id: 'B', stored: false, delivered: false, detail: "already watches 'nginx.service'" },
        { engine_id: 'C', stored: true, delivered: false, detail: 'this host is not connected' },
      ],
    };
    const { headline, problems } = fanoutSummary(result, (id) => `host-${id}`);
    expect(headline).toBe('Stored on 2 of 3 hosts.');
    // The undelivered host is listed beside the refused one: they are
    // different facts and both end with a machine showing no card.
    expect(problems).toEqual([
      "host-B: already watches 'nginx.service'",
      'host-C: this host is not connected',
    ]);
  });

  it('has nothing to report when every host took it', () => {
    const { headline, problems } = fanoutSummary(
      {
        group_id: 'g1',
        stored: 2,
        hosts: [
          { engine_id: 'A', stored: true, delivered: true, detail: null },
          { engine_id: 'B', stored: true, delivered: true, detail: null },
        ],
      },
      (id) => id,
    );
    expect(headline).toBe('Watching this on 2 hosts.');
    expect(problems).toEqual([]);
  });
});
