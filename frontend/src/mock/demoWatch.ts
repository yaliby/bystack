/**
 * Canned fleet + watch state for the mock canvas.
 *
 * The graph in `demoSnapshot.ts` shows what discovery would draw; this file is
 * the configuration side of ADR-0016 — what the operator asked for, including
 * the fan-out groups the ActionBar scopes over. Mutable so Add / Remove /
 * fan-out in the panel feel real without a Controller.
 */

import type {
  Actions,
  AuditEntry,
  CommandKind,
  CommandResult,
  EnrolledAgent,
  EnrollmentTerms,
  GroupCommandResult,
  Inventory,
  MatchKind,
  ProviderHealth,
  Urn,
  WatchEntry,
  WatchFanout,
  WatchKind,
  WatchList,
} from '../api/types';
import { scheduleDemoDiscovery, scheduleDemoDiscoveryMany } from './demoGraph';
import { DEMO_E1, DEMO_E2, DEMO_E3 } from './demoHosts';

const NOW = Math.floor(Date.now() / 1000);

export const NGINX_GROUP = 'g-nginx';
export const WORKER_GROUP = 'g-worker';
export const SSHD_GROUP = 'g-sshd';
export const POSTGRES_GROUP = 'g-postgres';

function unitUrn(engineId: string, name: string): Urn {
  return `bystack:unit:${engineId}/${name}`;
}

function processUrn(engineId: string, watchId: string): Urn {
  return `bystack:process:${engineId}/${watchId}`;
}

function seed(): WatchEntry[] {
  return [
    {
      id: 'w-nginx-e1',
      engine_id: DEMO_E1,
      kind: 'unit',
      group_id: NGINX_GROUP,
      group_hosts: 3,
      name: 'nginx.service',
      match_kind: null,
      pattern: '',
      label: 'nginx',
      added_at: NOW - 86_400,
      urn: unitUrn(DEMO_E1, 'nginx.service'),
    },
    {
      id: 'w-nginx-e2',
      engine_id: DEMO_E2,
      kind: 'unit',
      group_id: NGINX_GROUP,
      group_hosts: 3,
      name: 'nginx.service',
      match_kind: null,
      pattern: '',
      label: 'nginx',
      added_at: NOW - 86_400,
      urn: unitUrn(DEMO_E2, 'nginx.service'),
    },
    {
      id: 'w-nginx-e3',
      engine_id: DEMO_E3,
      kind: 'unit',
      group_id: NGINX_GROUP,
      group_hosts: 3,
      name: 'nginx.service',
      match_kind: null,
      pattern: '',
      label: 'nginx',
      added_at: NOW - 86_400,
      urn: unitUrn(DEMO_E3, 'nginx.service'),
    },
    {
      id: 'w-worker-e1',
      engine_id: DEMO_E1,
      kind: 'process',
      group_id: WORKER_GROUP,
      group_hosts: 2,
      name: '',
      match_kind: 'exec',
      pattern: '/usr/local/bin/order-worker',
      label: 'order-worker',
      added_at: NOW - 3_600,
      urn: processUrn(DEMO_E1, 'w-worker-e1'),
    },
    {
      id: 'w-worker-e2',
      engine_id: DEMO_E2,
      kind: 'process',
      group_id: WORKER_GROUP,
      group_hosts: 2,
      name: '',
      match_kind: 'exec',
      pattern: '/usr/local/bin/order-worker',
      label: 'order-worker',
      added_at: NOW - 3_600,
      urn: processUrn(DEMO_E2, 'w-worker-e2'),
    },
    {
      id: 'w-sshd-e1',
      engine_id: DEMO_E1,
      kind: 'unit',
      group_id: SSHD_GROUP,
      group_hosts: 1,
      name: 'sshd.service',
      match_kind: null,
      pattern: '',
      label: 'sshd',
      added_at: NOW - 7_200,
      urn: unitUrn(DEMO_E1, 'sshd.service'),
    },
    {
      id: 'w-postgres-e3',
      engine_id: DEMO_E3,
      kind: 'unit',
      group_id: POSTGRES_GROUP,
      group_hosts: 1,
      name: 'postgresql.service',
      match_kind: null,
      pattern: '',
      label: 'postgresql',
      added_at: NOW - 10_000,
      urn: unitUrn(DEMO_E3, 'postgresql.service'),
    },
  ];
}

let entries: WatchEntry[] = seed();
let audit: AuditEntry[] = [
  {
    id: 'a1',
    at: NOW - 120,
    actor: 'operator',
    kind: 'restart',
    target: unitUrn(DEMO_E1, 'nginx.service'),
    targets: [unitUrn(DEMO_E1, 'nginx.service')],
    status: 'succeeded',
    detail: null,
    reason: null,
    duration_ms: 410,
  },
  {
    id: 'a2',
    at: NOW - 118,
    actor: 'operator',
    kind: 'restart',
    target: unitUrn(DEMO_E2, 'nginx.service'),
    targets: [unitUrn(DEMO_E2, 'nginx.service')],
    status: 'succeeded',
    detail: null,
    reason: null,
    duration_ms: 380,
  },
  {
    id: 'a3',
    at: NOW - 115,
    actor: 'operator',
    kind: 'restart',
    target: unitUrn(DEMO_E3, 'nginx.service'),
    targets: [unitUrn(DEMO_E3, 'nginx.service')],
    status: 'failed',
    detail: 'Unit is not-found on this host',
    reason: null,
    duration_ms: 90,
  },
];

function recount(): void {
  const sizes = new Map<string, number>();
  for (const entry of entries) {
    sizes.set(entry.group_id, (sizes.get(entry.group_id) ?? 0) + 1);
  }
  entries = entries.map((entry) => ({
    ...entry,
    group_hosts: sizes.get(entry.group_id) ?? 1,
  }));
}

export function listWatchEntries(): readonly WatchEntry[] {
  return entries;
}

export function watchListFor(engineId: string): WatchList {
  return {
    engine_id: engineId,
    entries: entries.filter((entry) => entry.engine_id === engineId),
    delivered: true,
    detail: null,
  };
}

export function demoAgents(): readonly EnrolledAgent[] {
  return [
    {
      engine_id: DEMO_E1,
      status: 'local',
      certificate_expires_at: NOW + 365 * 86_400,
      enrolled_at: NOW - 30 * 86_400,
      last_seen: NOW,
      agent_version: '0.2.0',
      connected: true,
      local: true,
    },
    {
      engine_id: DEMO_E2,
      status: 'approved',
      certificate_expires_at: NOW + 365 * 86_400,
      enrolled_at: NOW - 14 * 86_400,
      last_seen: NOW - 2,
      agent_version: '0.2.0',
      connected: true,
      local: false,
    },
    {
      engine_id: DEMO_E3,
      status: 'approved',
      certificate_expires_at: NOW + 365 * 86_400,
      enrolled_at: NOW - 7 * 86_400,
      last_seen: NOW - 1,
      agent_version: '0.2.0',
      connected: true,
      local: false,
    },
    {
      engine_id: 'e4',
      status: 'pending',
      certificate_expires_at: NOW + 30 * 86_400,
      enrolled_at: NOW - 600,
      last_seen: 0,
      agent_version: '0.2.0',
      connected: false,
      local: false,
    },
  ];
}

export function demoTerms(): EnrollmentTerms {
  return {
    enabled: true,
    auto_approve: false,
    listen: '0.0.0.0:8443',
    upgrade:
      'curl -fsSL https://example.invalid/install-agent.sh | sudo sh -s -- --upgrade',
    local_agent: {
      state: 'running',
      detail: 'Managing this machine over a unix socket.',
    },
  };
}

export function demoProviders(): readonly ProviderHealth[] {
  return [
    {
      id: DEMO_E1,
      kind: 'agent',
      state: 'ready',
      detail: null,
      last_sync_at: NOW,
      node_count: 14,
      metrics: {},
    },
    {
      id: DEMO_E2,
      kind: 'agent',
      state: 'ready',
      detail: null,
      last_sync_at: NOW - 2,
      node_count: 4,
      metrics: {},
    },
    {
      id: DEMO_E3,
      kind: 'agent',
      state: 'ready',
      detail: null,
      last_sync_at: NOW - 1,
      node_count: 4,
      metrics: {},
    },
  ];
}

export function demoAudit(): readonly AuditEntry[] {
  return audit;
}

export function demoInventory(engineId: string, kind: WatchKind, filter: string): Inventory {
  const units = [
    {
      id: 'nginx.service',
      name: 'nginx.service',
      description: 'A high performance web server and a reverse proxy server',
      state: 'active',
      detail: 'running',
      pid: 1204,
    },
    {
      id: 'sshd.service',
      name: 'sshd.service',
      description: 'OpenSSH server daemon',
      state: 'active',
      detail: 'running',
      pid: 880,
    },
    {
      id: 'postgresql.service',
      name: 'postgresql.service',
      description: 'PostgreSQL RDBMS',
      state: engineId === DEMO_E3 ? 'active' : 'inactive',
      detail: engineId === DEMO_E3 ? 'running' : 'dead',
      pid: engineId === DEMO_E3 ? 2102 : 0,
    },
    {
      id: 'caddy.service',
      name: 'caddy.service',
      description: 'Caddy HTTP/2 web server',
      state: 'inactive',
      detail: 'dead',
      pid: 0,
    },
    {
      id: 'docker.service',
      name: 'docker.service',
      description: 'Docker Application Container Engine',
      state: 'active',
      detail: 'running',
      pid: 990,
    },
  ];
  const processes = [
    {
      id: '/usr/local/bin/order-worker',
      name: 'order-worker',
      description: '/usr/local/bin/order-worker --queue=orders',
      state: 'S',
      detail: '/usr/local/bin/order-worker --queue=orders',
      pid: 4412,
    },
    {
      id: '/usr/bin/redis-server',
      name: 'redis-server',
      description: '/usr/bin/redis-server *:6379',
      state: 'S',
      detail: '/usr/bin/redis-server *:6379',
      pid: 3011,
    },
    {
      id: 'python',
      name: 'python3',
      description: 'python3 api.py',
      state: 'S',
      detail: 'python3 api.py --port 8080',
      pid: 5520,
    },
  ];
  const items = (kind === 'unit' ? units : processes).filter((item) => {
    if (!filter) return true;
    const needle = filter.toLowerCase();
    return (
      item.id.toLowerCase().includes(needle) ||
      item.name.toLowerCase().includes(needle) ||
      item.description.toLowerCase().includes(needle)
    );
  });
  return {
    engine_id: engineId,
    kind,
    ok: true,
    reason: null,
    items,
    total: items.length,
  };
}

/** Actions the Controller would permit for a watched node on the demo map. */
export function demoActions(target: Urn, kind: string, status: string | null): Actions {
  if (kind === 'unit') {
    if (status === 'not-found' || status === 'masked' || status === 'error') {
      return {
        target,
        kind,
        targets: [target],
        commands: [],
        reason: 'unsupported_state',
        detail: 'Nothing can be done to a unit in this state.',
      };
    }
    if (status === 'inactive') {
      return {
        target,
        kind,
        targets: [target],
        commands: ['start', 'restart'],
        reason: null,
        detail: null,
      };
    }
    return {
      target,
      kind,
      targets: [target],
      commands: ['stop', 'restart', 'kill'],
      reason: null,
      detail: null,
    };
  }
  if (kind === 'process') {
    if (status === 'absent') {
      return {
        target,
        kind,
        targets: [target],
        commands: [],
        reason: 'unsupported_state',
        detail: 'No matching process is running.',
      };
    }
    return {
      target,
      kind,
      targets: [target],
      commands: ['stop', 'kill'],
      reason: null,
      detail: null,
    };
  }
  if (kind === 'service' || kind === 'container' || kind === 'stack') {
    return {
      target,
      kind,
      targets: [target],
      commands: ['stop', 'restart', 'kill'],
      reason: null,
      detail: null,
    };
  }
  return {
    target,
    kind,
    targets: [],
    commands: [],
    reason: 'unsupported_target',
    detail: null,
  };
}

function commandResult(kind: CommandKind, target: Urn): CommandResult {
  return {
    id: `cmd-${Math.random().toString(36).slice(2, 8)}`,
    kind,
    target,
    status: 'succeeded',
    actor: 'operator',
    requested_at: Date.now() / 1000,
    duration_ms: 240,
    outcomes: [{ target, status: 'succeeded', detail: null, duration_ms: 240 }],
  };
}

export function demoRunCommand(kind: CommandKind, target: Urn): CommandResult {
  const result = commandResult(kind, target);
  audit = [
    {
      id: result.id,
      at: result.requested_at,
      actor: result.actor,
      kind,
      target,
      targets: [target],
      status: result.status,
      detail: null,
      reason: null,
      duration_ms: result.duration_ms,
    },
    ...audit,
  ].slice(0, 50);
  // Discovery lags the command, same as a live agent: witness first, delta later.
  scheduleDemoDiscovery(kind, target);
  return result;
}

export function demoRunGroup(
  kind: CommandKind,
  groupId: string,
  engineIds: readonly string[],
): GroupCommandResult {
  const members = entries.filter(
    (entry) => entry.group_id === groupId && engineIds.includes(entry.engine_id),
  );
  const hosts = engineIds.map((engineId) => {
    const member = members.find((entry) => entry.engine_id === engineId);
    if (!member) {
      return {
        engine_id: engineId,
        urn: '' as const,
        ran: false,
        status: 'rejected',
        detail: 'This host is not in the group.',
        result: null,
      };
    }
    // nginx on shop-db is not-found in the canned graph — surface a failure
    // so the group result demonstrates worst-wins.
    if (member.urn === unitUrn(DEMO_E3, 'nginx.service') && kind !== 'start') {
      const failed = {
        ...commandResult(kind, member.urn),
        status: 'failed' as const,
        outcomes: [
          {
            target: member.urn,
            status: 'failed' as const,
            detail: 'Unit is not-found on this host',
            duration_ms: 40,
          },
        ],
      };
      audit = [
        {
          id: failed.id,
          at: failed.requested_at,
          actor: failed.actor,
          kind,
          target: member.urn,
          targets: [member.urn],
          status: 'failed' as const,
          detail: 'Unit is not-found on this host',
          reason: null,
          duration_ms: 40,
        },
        ...audit,
      ].slice(0, 50);
      return {
        engine_id: engineId,
        urn: member.urn,
        ran: true,
        status: 'failed',
        detail: 'Unit is not-found on this host',
        result: failed,
      };
    }
    const result = commandResult(kind, member.urn);
    audit = [
      {
        id: result.id,
        at: result.requested_at,
        actor: result.actor,
        kind,
        target: member.urn,
        targets: [member.urn],
        status: result.status,
        detail: null,
        reason: null,
        duration_ms: result.duration_ms,
      },
      ...audit,
    ].slice(0, 50);
    return {
      engine_id: engineId,
      urn: member.urn,
      ran: true,
      status: 'succeeded',
      detail: null,
      result,
    };
  });
  const status = hosts.some((host) => host.status === 'failed' || host.status === 'rejected')
    ? 'failed'
    : 'succeeded';
  const succeeded = hosts
    .filter((host) => host.ran && host.status === 'succeeded' && host.urn)
    .map((host) => host.urn as string);
  if (succeeded.length > 0) scheduleDemoDiscoveryMany(kind, succeeded);
  return { kind, group_id: groupId, status, hosts };
}

export function demoAddWatch(
  engineId: string,
  draft: {
    readonly kind: WatchKind;
    readonly name: string;
    readonly match_kind: MatchKind | null;
    readonly pattern: string;
    readonly label: string;
  },
): WatchList {
  const id = `w-${Math.random().toString(36).slice(2, 8)}`;
  const groupId = `g-${Math.random().toString(36).slice(2, 8)}`;
  const urn =
    draft.kind === 'unit'
      ? unitUrn(engineId, draft.name)
      : processUrn(engineId, id);
  entries = [
    ...entries,
    {
      id,
      engine_id: engineId,
      kind: draft.kind,
      group_id: groupId,
      group_hosts: 1,
      name: draft.name,
      match_kind: draft.match_kind,
      pattern: draft.pattern,
      label: draft.label,
      added_at: Date.now() / 1000,
      urn,
    },
  ];
  recount();
  return watchListFor(engineId);
}

export function demoAddAcross(
  draft: {
    readonly kind: WatchKind;
    readonly name: string;
    readonly match_kind: MatchKind | null;
    readonly pattern: string;
    readonly label: string;
  },
  engineIds: readonly string[],
): WatchFanout {
  const groupId = `g-${Math.random().toString(36).slice(2, 8)}`;
  const hosts = engineIds.map((engineId) => {
    const duplicate = entries.some(
      (entry) =>
        entry.engine_id === engineId &&
        ((draft.kind === 'unit' && entry.name === draft.name) ||
          (draft.kind === 'process' &&
            entry.match_kind === draft.match_kind &&
            entry.pattern === draft.pattern)),
    );
    if (duplicate) {
      return {
        engine_id: engineId,
        stored: false,
        delivered: false,
        detail: 'Already watching this on that host.',
      };
    }
    const id = `w-${Math.random().toString(36).slice(2, 8)}`;
    const urn =
      draft.kind === 'unit'
        ? unitUrn(engineId, draft.name)
        : processUrn(engineId, id);
    entries = [
      ...entries,
      {
        id,
        engine_id: engineId,
        kind: draft.kind,
        group_id: groupId,
        group_hosts: engineIds.length,
        name: draft.name,
        match_kind: draft.match_kind,
        pattern: draft.pattern,
        label: draft.label,
        added_at: Date.now() / 1000,
        urn,
      },
    ];
    return { engine_id: engineId, stored: true, delivered: true, detail: null };
  });
  recount();
  return {
    group_id: groupId,
    stored: hosts.filter((host) => host.stored).length,
    hosts,
  };
}

export function demoRemoveWatch(engineId: string, entryId: string): WatchList {
  entries = entries.filter((entry) => !(entry.engine_id === engineId && entry.id === entryId));
  recount();
  return watchListFor(engineId);
}
