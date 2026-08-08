import { describe, expect, it } from 'vitest';
import type {
  AgentStatus,
  EnrolledAgent,
  EnrollmentTerms,
  ProviderHealth,
} from '../../../api/types';
import {
  RENEWAL_WINDOW_MS,
  certificateNote,
  describeRow,
  describeSince,
  fleetRows,
  hostLabel,
  linkOf,
  localAgentNotice,
  pendingCount,
  revokeWarning,
  sameFleet,
  tokenLife,
} from './hosts';

const NOW = 1_800_000_000_000;
const NOW_UNIX = NOW / 1000;
const DAY = 24 * 3600;

function agent(engineId: string, overrides: Partial<EnrolledAgent> = {}): EnrolledAgent {
  return {
    engine_id: engineId,
    status: 'approved' as AgentStatus,
    certificate_expires_at: NOW_UNIX + 90 * DAY,
    enrolled_at: NOW_UNIX - 3600,
    last_seen: NOW_UNIX - 60,
    agent_version: '0.1.0',
    connected: true,
    local: false,
    ...overrides,
  };
}

function provider(id: string, state: ProviderHealth['state'], detail: string | null = null): ProviderHealth {
  return {
    id,
    kind: 'agent',
    state,
    detail,
    last_sync_at: NOW_UNIX - 60,
    node_count: 12,
    metrics: {},
  };
}

const noNames = () => null;

// ---------------------------------------------------------------------------
// status and connectivity are two facts, not one
// ---------------------------------------------------------------------------

describe('status and link stay separate', () => {
  it('reports an approved host that is asleep as approved and offline', () => {
    const [row] = fleetRows([agent('a', { connected: false })], [], noNames, NOW);

    expect(row.status).toBe('approved');
    expect(row.link.kind).toBe('offline');
  });

  it('reports a revoked host that is still streaming as revoked and connected', () => {
    // Revocation is an allow-list check at the *next* connection, so this is
    // the ordinary state for a few seconds after the click. A panel that drew
    // it as gone would contradict the containers still arriving on the canvas.
    const [row] = fleetRows(
      [agent('a', { status: 'revoked', connected: true })],
      [provider('a', 'ready')],
      noNames,
      NOW,
    );

    expect(row.status).toBe('revoked');
    expect(row.link.kind).toBe('connected');
  });

  it('reports a pending host that has never dialled as pending and never connected', () => {
    const [row] = fleetRows(
      [agent('a', { status: 'pending', connected: false, last_seen: 0 })],
      [],
      noNames,
      NOW,
    );

    expect(row.status).toBe('pending');
    expect(row.link.kind).toBe('never');
  });
});

// ---------------------------------------------------------------------------
// degraded is stale, not absent
// ---------------------------------------------------------------------------

describe('a disconnected host with a retained partition', () => {
  it('is stale rather than gone, and carries the provider’s own reason', () => {
    const link = linkOf(agent('a', { connected: false }), provider('a', 'degraded', 'stream closed'));

    expect(link).toEqual({ kind: 'stale', detail: 'stream closed' });
  });

  it('is told apart from a host that never connected at all', () => {
    // Same `connected: false`. Opposite advice: one is "its topology is old",
    // the other is "go and check that the agent is running".
    const fresh = { connected: false, last_seen: 0 };
    expect(linkOf(agent('a', fresh), undefined).kind).toBe('never');
    expect(linkOf(agent('a', fresh), provider('a', 'stopped')).kind).toBe('never');
  });

  it('does not call a host new because the Controller restarted', () => {
    // The graph is in memory and the enrollment record is not. After a
    // restart every host that has not dialled back yet has no provider — and
    // a fleet that all reads "never connected" is a lie the durable half of
    // the system can disprove.
    const known = agent('a', { connected: false, last_seen: NOW_UNIX - 600 });

    expect(linkOf(known, undefined)).toEqual({ kind: 'offline' });
    expect(describeRow(fleetRows([known], [], noNames, NOW)[0], '10m ago')).toMatch(
      /last seen 10m ago/,
    );
  });

  it('is told apart from a refused certificate', () => {
    const link = linkOf(agent('a', { connected: false }), provider('a', 'failed', 'revoked'));

    expect(link).toEqual({ kind: 'failed', detail: 'revoked' });
  });

  it('trusts the list over a provider mid-handshake', () => {
    // `connected` is computed from the live provider set on the same request;
    // a provider that is STARTING with no session bound has nothing to show,
    // and the two lists are polled separately so they can disagree by a beat.
    expect(
      linkOf(agent('a', { connected: false, last_seen: 0 }), provider('a', 'starting')).kind,
    ).toBe('never');
    expect(linkOf(agent('a', { connected: true }), provider('a', 'degraded')).kind).toBe('connected');
  });
});

// ---------------------------------------------------------------------------
// what a row tells the operator to do
// ---------------------------------------------------------------------------

describe('the row’s sentence', () => {
  const rowFor = (agentOverrides: Partial<EnrolledAgent>, providers: ProviderHealth[] = []) =>
    fleetRows([agent('a', agentOverrides)], providers, noNames, NOW)[0];

  it('sends a pending host to the approve button, not to the machine room', () => {
    // A pending agent is refused at Hello, so it never has a session bound and
    // "never connected" is true of every one of them. Saying "check that the
    // agent is running" would send an operator to a host that is running,
    // dialling, and being turned away by the button beside the sentence.
    const row = rowFor({ status: 'pending', connected: false, last_seen: 0 });

    expect(row.link.kind).toBe('never');
    expect(describeRow(row, null)).toMatch(/Approve it/);
    expect(describeRow(row, null)).not.toMatch(/agent is running/);
  });

  it('still tells an approved host that has never dialled to go and look', () => {
    const row = rowFor({ connected: false, last_seen: 0 });

    expect(describeRow(row, null)).toMatch(/agent is running/);
  });

  it('describes a degraded host by what is still on the canvas', () => {
    const row = rowFor({ connected: false }, [provider('a', 'degraded', 'stream closed')]);
    const sentence = describeRow(row, '4m ago');

    expect(sentence).toMatch(/still on the canvas/);
    expect(sentence).toMatch(/4m ago/);
    expect(sentence).toMatch(/stream closed/);
    // Stale, not gone and not broken.
    expect(sentence).not.toMatch(/error|failed|unavailable/i);
  });
});

// ---------------------------------------------------------------------------
// ordering: pending first
// ---------------------------------------------------------------------------

describe('ordering', () => {
  it('puts pending hosts first, then approved, then revoked', () => {
    const rows = fleetRows(
      [
        agent('approved-one', { status: 'approved' }),
        agent('revoked-one', { status: 'revoked' }),
        agent('pending-one', { status: 'pending' }),
      ],
      [],
      noNames,
      NOW,
    );

    expect(rows.map((row) => row.status)).toEqual(['pending', 'approved', 'revoked']);
  });

  it('puts the most recently enrolled pending host at the very top', () => {
    // The operator has just pasted a command onto a machine. That row is the
    // one they are looking for, and it should not have to be hunted for.
    const rows = fleetRows(
      [
        agent('old', { status: 'pending', enrolled_at: NOW_UNIX - 900 }),
        agent('just-now', { status: 'pending', enrolled_at: NOW_UNIX - 2 }),
      ],
      [],
      noNames,
      NOW,
    );

    expect(rows.map((row) => row.agent.engine_id)).toEqual(['just-now', 'old']);
  });

  it('sorts the settled fleet by name, so the inventory is scannable', () => {
    const names: Record<string, string> = { e1: 'web-02', e2: 'web-01' };
    const rows = fleetRows(
      [agent('e1'), agent('e2')],
      [],
      (engineId) => names[engineId] ?? null,
      NOW,
    );

    expect(rows.map((row) => row.label)).toEqual(['web-01', 'web-02']);
  });

  it('counts the hosts waiting on a person', () => {
    expect(
      pendingCount([
        agent('a', { status: 'pending' }),
        agent('b', { status: 'approved' }),
        agent('c', { status: 'pending' }),
      ]),
    ).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// labelling
// ---------------------------------------------------------------------------

describe('labelling', () => {
  it('prefers the name the host calls itself', () => {
    expect(hostLabel('9f2c8b1e4a6d5c3b', 'lab-node-01')).toBe('lab-node-01');
  });

  it('shortens an engine id when discovery has not produced a name', () => {
    // A pending host has no node in the graph, by design: it contributes
    // nothing until approved.
    expect(hostLabel('9f2c8b1e4a6d5c3b7a8e', null)).toBe('9f2c8b1e4a6d…');
    expect(hostLabel('short', null)).toBe('short');
  });
});

// ---------------------------------------------------------------------------
// certificate expiry
// ---------------------------------------------------------------------------

describe('certificate expiry', () => {
  it('says nothing about a healthy certificate', () => {
    expect(certificateNote(agent('a'), NOW)).toBeNull();
  });

  it('says nothing while the agent is connected, because renewal is automatic', () => {
    const soon = agent('a', {
      connected: true,
      certificate_expires_at: NOW_UNIX + 5 * DAY,
    });

    expect(certificateNote(soon, NOW)).toBeNull();
  });

  it('warns when an offline agent is inside its renewal window', () => {
    // Renewal is offered over the stream that is already open. An agent that
    // is not there cannot take the offer, and the fix is to start it.
    const stranded = agent('a', {
      connected: false,
      certificate_expires_at: NOW_UNIX + RENEWAL_WINDOW_MS / 1000 - DAY,
    });

    expect(certificateNote(stranded, NOW)).toMatch(/expires 29d from now/);
  });

  it('says an expired certificate needs a new enrollment, connected or not', () => {
    const dead = agent('a', { connected: true, certificate_expires_at: NOW_UNIX - 60 });

    expect(certificateNote(dead, NOW)).toMatch(/enrol again with a new token/);
  });
});

// ---------------------------------------------------------------------------
// the join token
// ---------------------------------------------------------------------------

describe('token expiry', () => {
  it('counts down in mm:ss', () => {
    expect(tokenLife(NOW_UNIX + 900, NOW).clock).toBe('15:00');
    expect(tokenLife(NOW_UNIX + 61, NOW).clock).toBe('1:01');
    expect(tokenLife(NOW_UNIX + 9, NOW).clock).toBe('0:09');
  });

  it('switches units rather than printing a four-digit minute count', () => {
    expect(tokenLife(NOW_UNIX + 3 * 3600 + 300, NOW).clock).toBe('3h 5m');
  });

  it('is expired at the boundary and after it', () => {
    expect(tokenLife(NOW_UNIX, NOW)).toEqual({ remainingMs: 0, expired: true, clock: 'expired' });
    expect(tokenLife(NOW_UNIX - 3600, NOW).expired).toBe(true);
  });

  it('never reports a negative remainder a caller could render', () => {
    expect(tokenLife(NOW_UNIX - 1, NOW).remainingMs).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// revoking
// ---------------------------------------------------------------------------

describe('the revoke confirmation', () => {
  it('names the host, so a list of near-identical rows is not a coin flip', () => {
    const [row] = fleetRows([agent('e1', { connected: false })], [], () => 'db-primary', NOW);

    expect(revokeWarning(row)).toMatch(/^Revoke db-primary\?/);
    expect(revokeWarning(row)).toMatch(/new join token/);
  });

  it('warns that a connected host keeps its current stream', () => {
    const [row] = fleetRows([agent('e1')], [provider('e1', 'ready')], () => 'db-primary', NOW);

    expect(revokeWarning(row)).toMatch(/stays connected until that stream drops/);
  });
});

// ---------------------------------------------------------------------------
// polling hygiene
// ---------------------------------------------------------------------------

describe('change detection', () => {
  it('ignores a heartbeat, which moves last_seen and nothing else', () => {
    expect(sameFleet([agent('a')], [agent('a', { last_seen: NOW_UNIX })])).toBe(true);
  });

  it('notices an approval, a disconnect, an upgrade and a new host', () => {
    expect(sameFleet([agent('a', { status: 'pending' })], [agent('a')])).toBe(false);
    expect(sameFleet([agent('a')], [agent('a', { connected: false })])).toBe(false);
    expect(sameFleet([agent('a')], [agent('a', { agent_version: '0.2.0' })])).toBe(false);
    expect(sameFleet([agent('a')], [agent('a'), agent('b')])).toBe(false);
  });

  it('notices a renewal, which is the only sign of one a browser gets', () => {
    expect(
      sameFleet([agent('a')], [agent('a', { certificate_expires_at: NOW_UNIX + 120 * DAY })]),
    ).toBe(false);
  });
});

describe('relative time', () => {
  it('is coarse and past tense', () => {
    expect(describeSince(NOW_UNIX - 45, NOW)).toBe('45s ago');
    expect(describeSince(NOW_UNIX - 300, NOW)).toBe('5m ago');
    expect(describeSince(NOW_UNIX - 5 * 3600, NOW)).toBe('5h ago');
    expect(describeSince(NOW_UNIX - 5 * DAY, NOW)).toBe('5d ago');
  });

  it('has nothing to say about a host that has never been seen', () => {
    // `last_seen` is 0 until a session happens. "56 years ago" is worse than
    // silence, and the row says "never connected" on its own.
    expect(describeSince(0, NOW)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// this machine, which was never enrolled
// ---------------------------------------------------------------------------

function local(overrides: Partial<EnrolledAgent> = {}): EnrolledAgent {
  return agent('this-machine', {
    status: 'local',
    local: true,
    // No certificate, so no expiry -- the Controller reports zero rather than
    // inventing a date (`api/routes/enrollment.py`, `AgentOut.local`).
    certificate_expires_at: 0,
    connected: true,
    ...overrides,
  });
}

function terms(overrides: Partial<EnrollmentTerms> = {}): EnrollmentTerms {
  return {
    enabled: false,
    auto_approve: false,
    listen: '0.0.0.0:8443',
    local_agent: { state: 'running', detail: '' },
    ...overrides,
  };
}

describe('the local host is not an enrolled host', () => {
  it('never claims its certificate expired', () => {
    // The regression this exists to stop: an expiry of zero is in the past, so
    // the generic path tells the operator their own machine has to enrol again
    // with a new token -- advice for a host that never enrolled and never will.
    expect(certificateNote(local(), NOW)).toBeNull();
  });

  it('says where the switch is instead of offering approval', () => {
    const [row] = fleetRows([local()], [], noNames, NOW);

    expect(row.status).toBe('local');
    expect(describeRow(row, null)).toContain('never enrolled');
    expect(describeRow(row, null)).toContain('local_agent.enabled');
    // Not the fleet's workflow: nothing here is waiting on a person, and
    // "approve it and it will be along within seconds" would be a lie.
    expect(describeRow(row, null)).not.toContain('Approve it');
  });

  it('does not count towards the pending badge', () => {
    // The badge means "somebody has to click approve". Nothing about this row
    // is waiting on a person.
    expect(pendingCount([local(), agent('b', { status: 'pending' })])).toBe(1);
  });

  it('sorts under a host that is waiting for approval and above the fleet', () => {
    const rows = fleetRows(
      [agent('zeta'), local(), agent('beta', { status: 'pending' })],
      [],
      noNames,
      NOW,
    );

    expect(rows.map((row) => row.status)).toEqual(['pending', 'local', 'approved']);
  });
});

// ---------------------------------------------------------------------------
// explaining an empty first run
// ---------------------------------------------------------------------------

describe('what to say when this machine is not on the list', () => {
  it('says nothing while the local agent is running and has a row', () => {
    const rows = fleetRows([local()], [], noNames, NOW);
    expect(localAgentNotice(terms(), rows)).toBeNull();
  });

  it('passes on the Controller’s reason when the agent could not start', () => {
    const notice = localAgentNotice(
      terms({
        local_agent: {
          state: 'unavailable',
          detail: 'no Docker socket at /var/run/docker.sock.',
        },
      }),
      [],
    );

    expect(notice).toContain('/var/run/docker.sock');
  });

  it('mentions being switched off only when it is why the map is blank', () => {
    const off = terms({ local_agent: { state: 'disabled', detail: 'local_agent.enabled is false' } });

    expect(localAgentNotice(off, [])).toContain('local_agent.enabled');
    // With a fleet on the canvas, a deliberate setting is not news.
    expect(localAgentNotice(off, fleetRows([agent('a')], [], noNames, NOW))).toBeNull();
  });

  it('claims nothing before the Controller has answered', () => {
    expect(localAgentNotice(null, [])).toBeNull();
  });
});

describe('a host that is enrolled AND managed locally', () => {
  // Found by running the Controller on a machine that had been enrolled into
  // its own fleet earlier: the host appeared twice, and the second row claimed
  // the first one's connection because `connected` comes from the one provider
  // they share. The backend now merges on the engine id; this is the panel's
  // half of that.
  const both = () => agent('this-machine', { local: true, status: 'approved' });

  it('keeps its enrollment status, because that certificate still exists', () => {
    const [row] = fleetRows([both()], [], noNames, NOW);

    expect(row.status).toBe('approved');
    expect(row.agent.local).toBe(true);
  });

  it('says both facts, because the operator needs both', () => {
    const [row] = fleetRows([both()], [], noNames, NOW);
    const sentence = describeRow(row, null);

    expect(sentence).toMatch(/its own machine/);
    expect(sentence).toMatch(/also enrolled/);
    // The certificate is idle, not gone — it is still the way in for anyone
    // holding it, which is why the row keeps its revoke button.
    expect(sentence).toMatch(/idle/);
  });

  it('still warns about a certificate approaching expiry', () => {
    // Unlike an unenrolled local host, this one has a certificate — and it
    // cannot renew, because renewal happens over a connection the local agent
    // is holding instead.
    const expiring = agent('this-machine', {
      local: true,
      connected: false,
      certificate_expires_at: NOW_UNIX + 3 * DAY,
    });

    expect(certificateNote(expiring, NOW)).toMatch(/expires/);
  });

  it('suppresses the empty-machine notice, since this machine is managed', () => {
    const rows = fleetRows([both()], [], noNames, NOW);
    expect(localAgentNotice(terms(), rows)).toBeNull();
  });
});
