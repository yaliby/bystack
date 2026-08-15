import { describe, expect, it } from 'vitest';
import type { GroupCommandResult, Urn, WatchEntry } from '../../../api/types';
import { groupCommandSummary, indexGroups, scopeHosts, scopeLabel, scopeMarks } from './groups';

function entry(overrides: Partial<WatchEntry> = {}): WatchEntry {
  return {
    id: 'w1',
    engine_id: 'E1',
    kind: 'unit',
    group_id: 'g1',
    group_hosts: 1,
    name: 'nginx.service',
    match_kind: null,
    pattern: '',
    label: '',
    added_at: 0,
    urn: 'bystack:unit:E1/nginx.service',
    ...overrides,
  };
}

describe('finding the other machines a card was chosen alongside', () => {
  const entries = [
    entry({ id: 'a', engine_id: 'E1', urn: 'bystack:unit:E1/nginx.service' }),
    entry({ id: 'b', engine_id: 'E2', urn: 'bystack:unit:E2/nginx.service' }),
    entry({ id: 'c', engine_id: 'E3', urn: 'bystack:unit:E3/nginx.service' }),
    // A different act of selection, same unit name. Not the same group.
    entry({ id: 'd', engine_id: 'E4', group_id: 'g2', urn: 'bystack:unit:E4/nginx.service' }),
  ];

  it('points every member at the same group, whichever one was clicked', () => {
    const index = indexGroups(entries, () => null);
    const from1 = index.get('bystack:unit:E1/nginx.service' as Urn);
    const from3 = index.get('bystack:unit:E3/nginx.service' as Urn);
    expect(from1).toBe(from3);
    expect(from1?.members.map((m) => m.engineId)).toEqual(['E1', 'E2', 'E3']);
  });

  it('does not join two selections that merely name the same unit', () => {
    // The group is the act of choosing, not the thing chosen. Watching nginx
    // on E4 separately is a different decision and stays one.
    const index = indexGroups(entries, () => null);
    expect(index.get('bystack:unit:E4/nginx.service' as Urn)?.members).toHaveLength(1);
  });

  it('keeps a group of one, because watched and grouped are different questions', () => {
    const index = indexGroups([entry()], () => null);
    expect(index.get('bystack:unit:E1/nginx.service' as Urn)?.members).toHaveLength(1);
  });

  it('calls a machine what the graph calls it, not by its engine id', () => {
    const index = indexGroups([entry()], (id) => (id === 'E1' ? 'lab-node-01' : null));
    expect(index.get('bystack:unit:E1/nginx.service' as Urn)?.members[0].label).toBe('lab-node-01');
  });
});

describe('choosing which machines a press reaches', () => {
  const group = {
    groupId: 'g1',
    members: [
      { engineId: 'E1', urn: 'bystack:unit:E1/nginx.service' as Urn, label: 'a' },
      { engineId: 'E2', urn: 'bystack:unit:E2/nginx.service' as Urn, label: 'b' },
      { engineId: 'E3', urn: 'bystack:unit:E3/nginx.service' as Urn, label: 'c' },
    ],
  };

  it('reaches only this machine by default', () => {
    expect(scopeHosts(group, { mode: 'one' }, 'E2')).toEqual(['E2']);
  });

  it('reaches every member when the operator says so', () => {
    expect(scopeHosts(group, { mode: 'all' }, 'E2')).toEqual(['E1', 'E2', 'E3']);
  });

  it('always includes the machine whose card is open', () => {
    // A scope that silently excluded it would act on everything except the
    // thing being looked at.
    expect(scopeHosts(group, { mode: 'some', engineIds: ['E3'] }, 'E2')).toEqual(['E2', 'E3']);
    // And never twice, however it was ticked.
    expect(scopeHosts(group, { mode: 'some', engineIds: ['E2', 'E3'] }, 'E2')).toEqual([
      'E2',
      'E3',
    ]);
  });

  it('says how many machines, so the button is not a surprise', () => {
    expect(scopeLabel(1)).toBe('this host');
    expect(scopeLabel(3)).toBe('3 hosts');
  });

  it('lights nothing while the scope is still this host', () => {
    // Ordinary selection already answers that question; painting it again
    // would just dim the rest of the map for no reason.
    expect(scopeMarks(group, { mode: 'one' }, 'E2')).toEqual([]);
  });

  it('lights every member when the operator says all', () => {
    expect(scopeMarks(group, { mode: 'all' }, 'E2')).toEqual([
      'bystack:unit:E1/nginx.service',
      'bystack:unit:E2/nginx.service',
      'bystack:unit:E3/nginx.service',
    ]);
  });

  it('lights only the machines that were ticked under choose', () => {
    expect(scopeMarks(group, { mode: 'some', engineIds: ['E3'] }, 'E2')).toEqual([
      'bystack:unit:E2/nginx.service',
      'bystack:unit:E3/nginx.service',
    ]);
  });
});

describe('what a command across hosts has to say for itself', () => {
  it('names the machines that are not in the state that was asked for', () => {
    // One asleep and one that ran and failed. Different facts, same list:
    // both end with a machine the operator has to go and look at.
    const result: GroupCommandResult = {
      kind: 'restart',
      group_id: 'g1',
      status: 'failed',
      hosts: [
        { engine_id: 'E1', urn: 'bystack:unit:E1/n.service', ran: true, status: 'succeeded', detail: null, result: null },
        { engine_id: 'E2', urn: '', ran: false, status: 'provider_unavailable', detail: 'no connected agent owns this host right now', result: null },
        { engine_id: 'E3', urn: 'bystack:unit:E3/n.service', ran: true, status: 'failed', detail: 'unit not found', result: null },
      ],
    };
    const { headline, problems, ok } = groupCommandSummary(result);
    expect(ok).toBe(false);
    expect(headline).toBe('2 of 3 hosts acted on.');
    expect(problems).toEqual([
      'E2: no connected agent owns this host right now',
      'E3: unit not found',
    ]);
  });

  it('treats already-in-that-state as done rather than as a problem', () => {
    const { ok, problems } = groupCommandSummary({
      kind: 'start',
      group_id: 'g1',
      status: 'noop',
      hosts: [
        { engine_id: 'E1', urn: 'u' as Urn, ran: true, status: 'noop', detail: null, result: null },
        { engine_id: 'E2', urn: 'u2' as Urn, ran: true, status: 'succeeded', detail: null, result: null },
      ],
    });
    expect(ok).toBe(true);
    expect(problems).toEqual([]);
  });
});
