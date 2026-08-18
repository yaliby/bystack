/**
 * What the hosts panel is allowed to say about a fleet.
 *
 * Pure: no React, no network. Everything interesting here is a consequence of
 * two backend decisions that a panel gets wrong by default, so both live where
 * they can be tested without a browser.
 *
 * The first is that **`status` and `connected` are separate facts** and stay
 * separate (`api/routes/enrollment.py`). An agent can be approved and asleep,
 * or revoked and still streaming until its connection drops. Folding them into
 * one badge forces a lie in whichever direction the fold went — telling an
 * operator to approve a host that is already approved, or drawing a host as
 * gone while its containers are still arriving.
 *
 * The second is that **a disconnected host keeps its topology**. `DEGRADED`
 * means "the last known picture, and no agent attached" (ARCHITECTURE §1, the
 * ProviderState table in docs/MIGRATION.md §2). That is stale, not absent and
 * not an error, and the difference between it and a host that never connected
 * at all is the difference between "wait" and "go and look at the machine".
 */

import type {
  AgentStatus,
  EnrolledAgent,
  EnrollmentTerms,
  ProviderHealth,
} from '../../../api/types';

/**
 * Whether this host is on the stream, and if not, what is left of it.
 *
 * Derived from the agent list and the provider health the app already polls —
 * `connected` alone cannot tell "disconnected, graph retained" from "enrolled
 * and never seen", and those want opposite words.
 */
export type Link =
  /** On the stream now. Whatever the canvas shows for this host is live. */
  | { readonly kind: 'connected' }
  /** Gone, but its topology is still on the canvas and still worth reading. */
  | { readonly kind: 'stale'; readonly detail: string | null }
  /** The Controller refused it: a revoked or rejected certificate. */
  | { readonly kind: 'failed'; readonly detail: string | null }
  /** Away, with nothing of it left on the canvas — the graph is in memory. */
  | { readonly kind: 'offline' }
  /** Enrolled, and has never had a session. Usually: the agent is not running. */
  | { readonly kind: 'never' };

export interface HostRow {
  readonly agent: EnrolledAgent;
  /** The host's name from the graph when we have one, else a short engine id. */
  readonly label: string;
  readonly status: AgentStatus;
  readonly link: Link;
  /** A condition worth naming on the row itself, or `null`. */
  readonly note: string | null;
}

/** Provider states in which an agent is attached and serving. Mirrors the route. */
const LIVE_STATES: ReadonlySet<ProviderHealth['state']> = new Set([
  'starting',
  'syncing',
  'ready',
]);

export const STATUS_LABEL: Record<AgentStatus, string> = {
  pending: 'Awaiting approval',
  approved: 'Approved',
  revoked: 'Revoked',
  local: 'This machine',
};

export const LINK_LABEL: Record<Link['kind'], string> = {
  connected: 'Connected',
  stale: 'Stale',
  failed: 'Refused',
  offline: 'Offline',
  never: 'Never connected',
};

/**
 * What this row is actually telling the operator to do next.
 *
 * Status first, because a pending host is a special case that the link alone
 * gets wrong: it is refused at `Hello` and so never has a session bound to it,
 * which makes "never connected" true, unhelpful, and — if it were phrased as
 * "check that the agent is running" — actively misleading. The agent is very
 * probably running, dialling, and being turned away by the approval that is
 * one button to the right.
 *
 * `stale` then deliberately describes what is still true rather than what is
 * missing: the topology on the canvas came from this host and is the last
 * thing it said. Calling it "down" would invite an operator to disbelieve a
 * picture that is merely old.
 */
export function describeRow(row: HostRow, lastSeen: string | null): string {
  // This machine, managed by the agent the Controller spawned for it. It has
  // no enrollment and no certificate, so every sentence below — approval,
  // renewal, "check the agent is running on the host" — is about a fleet this
  // row is not part of. It says what it is and where the switch is instead.
  if (row.status === 'local') {
    return row.link.kind === 'connected'
      ? 'Managed by the agent this Controller runs for its own machine. It was never enrolled, so there is nothing here to approve or revoke — turn it off with `local_agent.enabled`.'
      : 'The local agent is not on the stream. The Controller restarts it on a backoff; its last words are in the Hosts header.';
  }

  // Enrolled *and* managed locally: this machine was added to the fleet at
  // some point, and the Controller now runs on it. Both facts are true and
  // the operator needs both — the certificate is idle but still valid, and
  // still the way in for anyone holding it, which is what makes the revoke
  // button on this row worth keeping.
  if (row.agent.local) {
    return `Managed by the agent this Controller runs for its own machine. It is also enrolled in the fleet${
      row.status === 'revoked' ? ', and revoked' : ''
    }; that certificate is idle while the local agent holds the stream.`;
  }

  if (row.status === 'pending') {
    return row.link.kind === 'connected'
      ? 'On the stream, and contributing nothing until it is approved.'
      : 'It has its certificate and is retrying on a backoff. Approve it and it will be along within seconds.';
  }

  switch (row.link.kind) {
    case 'connected':
      return 'Streaming to the Controller.';
    case 'stale':
      return `Disconnected${lastSeen ? ` ${lastSeen}` : ''}. The topology it reported is still on the canvas, and is the last thing it said.${
        row.link.detail ? ` ${row.link.detail}` : ''
      }`;
    case 'failed':
      return row.link.detail ?? 'The Controller is refusing this agent’s certificate.';
    case 'offline':
      return `Not connected${lastSeen ? `, last seen ${lastSeen}` : ''}. Nothing of this host is on the canvas.`;
    case 'never':
      return row.status === 'revoked'
        ? 'Revoked, and it never connected.'
        : 'Approved, but it has never connected. Check that the agent is running on the host.';
  }
}

export function linkOf(agent: EnrolledAgent, provider: ProviderHealth | undefined): Link {
  // `connected` is the Controller's own answer to "is there a session right
  // now", and it is the one field computed from the live provider set rather
  // than from a stored record. It wins.
  if (agent.connected) return { kind: 'connected' };
  if (provider && provider.state === 'failed') {
    return { kind: 'failed', detail: provider.detail };
  }
  if (provider && !LIVE_STATES.has(provider.state) && provider.state !== 'stopped') {
    return { kind: 'stale', detail: provider.detail };
  }
  // No partition on the Controller: no provider at all, one that never
  // started, or one mid-handshake with nothing bound to it yet. Whether this
  // host has *ever* been seen is the enrollment record's to answer, and it is
  // the durable half — the graph is in memory and does not survive a restart,
  // so an old host on a freshly-restarted Controller looks exactly like a new
  // one here and must not be described as one.
  return agent.last_seen ? { kind: 'offline' } : { kind: 'never' };
}

/**
 * The final third of a certificate's life, in milliseconds.
 *
 * Renewal is offered by the Controller at two thirds elapsed, over the stream
 * that is already open (ADR-0011) — so an agent that is *offline* in this
 * window cannot renew, and will need a new join token if it stays away. That
 * is the only certificate condition worth putting on a row, and it is worth
 * putting there because the fix (start the agent) is not obvious from
 * "expired".
 *
 * A third of the default 90-day lifetime. The wire carries the expiry but not
 * the lifetime it was issued for, so a configured `cert_ttl_days` moves the
 * real renewal point without moving this; it is a warning threshold, and being
 * early is the harmless direction.
 */
export const RENEWAL_WINDOW_MS = 30 * 24 * 3600 * 1000;

export function certificateNote(agent: EnrolledAgent, now: number): string | null {
  // The local agent has no certificate, and reports the expiry as zero rather
  // than inventing one. Without this line that zero is in the past, and the
  // row for the operator's own machine reads "its certificate has expired,
  // this host has to enrol again" — advice for a host that never enrolled and
  // never will.
  if (agent.status === 'local') return null;

  const remaining = agent.certificate_expires_at * 1000 - now;
  if (remaining <= 0) {
    return 'Its certificate has expired. This host has to enrol again with a new token.';
  }
  if (agent.connected || remaining > RENEWAL_WINDOW_MS) return null;
  return `Its certificate expires ${describeDuration(remaining)} from now. Renewal happens over the agent’s own connection, so it has to come back before then.`;
}

/**
 * Whether this host is running something other than the Controller's version.
 *
 * String inequality, not a semver comparison. Deciding what `0.2.0-rc1` is
 * relative to `0.2.0` is a question this panel has no stake in, and every
 * wrong guess shows a host as current when it is not. "Different from the
 * Controller" is what an operator is actually asking, and it has no edge
 * cases.
 *
 * An agent that has never connected reports no version at all — unknown, not
 * behind. Saying otherwise would put every host that happens to be powered off
 * into a list of things to go and fix. `null` for the Controller's version
 * means the health poll has not answered yet, which is the same situation.
 *
 * A mixed-version fleet is an ordinary operating state (ADR-0008), so this is
 * a *report* and not a fault, and there is no button on the card. What to do
 * about it is named once for the whole panel and depends on the Controller
 * rather than on the host: one holding a signed release pushes it (ADR-0017),
 * one holding nothing hands out the installer line (ADR-0015). See
 * `model/upgrade.ts`.
 */
export function versionSkew(agent: EnrolledAgent, controller: string | null): boolean {
  return Boolean(controller) && Boolean(agent.agent_version) &&
    agent.agent_version !== controller;
}

/**
 * How many hosts are running something other than the Controller.
 *
 * Exists so the upgrade command can be shown *once*, above a list, rather than
 * repeated on every card that differs. The command is a constant — the same
 * line runs on all of them — and a constant printed per row is the shape that
 * turns a four-host fleet into a screen of the same sentence. Which hosts they
 * are is already on the cards; this answers whether to say anything at all.
 *
 * Not `behindCount`, and the name is the point: `versionSkew` takes no view on
 * which number is larger, so a host somebody upgraded before the Controller is
 * in this count too. A name that claimed "behind" is how the panel came to say
 * "running an older agent" over a host running a newer one.
 */
export function skewCount(rows: readonly HostRow[], controller: string | null): number {
  return rows.filter((row) => versionSkew(row.agent, controller)).length;
}

export function hostLabel(engineId: string, name: string | null): string {
  if (name) return name;
  // Engine ids are 64 hex characters or a colon-delimited fingerprint, and
  // neither is something a person recognises. Enough of the prefix to tell two
  // rows apart; the full value is one hover away in the panel.
  return engineId.length > 14 ? `${engineId.slice(0, 12)}…` : engineId;
}

/**
 * Group order: pending, then approved, then revoked.
 *
 * Not alphabetical and not by connection. An operator opens this panel for one
 * of two reasons, and by far the more common is that they have just pasted the
 * install command onto a machine and are waiting for the row to appear. That
 * row must be the first thing in the list, every time, without being hunted
 * for.
 */
const GROUP: Record<AgentStatus, number> = { pending: 0, local: 1, approved: 2, revoked: 3 };

/**
 * The panel's whole view model, in the order it is rendered.
 *
 * `resolveName` is how a host stops being a hex string: the graph already
 * holds a `host` node per engine id, carrying the name the machine calls
 * itself. Passed in rather than reached for, so this stays testable.
 */
export function fleetRows(
  agents: readonly EnrolledAgent[],
  providers: readonly ProviderHealth[],
  resolveName: (engineId: string) => string | null,
  now: number,
): HostRow[] {
  const byId = new Map(providers.map((provider) => [provider.id, provider]));
  const rows = agents.map((agent) => ({
    agent,
    label: hostLabel(agent.engine_id, resolveName(agent.engine_id)),
    status: agent.status,
    link: linkOf(agent, byId.get(agent.engine_id)),
    note: certificateNote(agent, now),
  }));

  return rows.sort((a, b) => {
    const group = GROUP[a.status] - GROUP[b.status];
    if (group !== 0) return group;
    // Within the pending group, newest first: the one just enrolled is the one
    // being looked for. Everywhere else the list is a fleet inventory, and
    // alphabetical is what makes it scannable.
    if (a.status === 'pending') return b.agent.enrolled_at - a.agent.enrolled_at;
    return a.label.localeCompare(b.label);
  });
}

export function pendingCount(agents: readonly EnrolledAgent[]): number {
  return agents.filter((agent) => agent.status === 'pending').length;
}

/**
 * What to say about this machine when it is not on the list.
 *
 * The panel's first job on a first run is explaining an empty canvas. Three
 * causes render as the same nothing — no Docker socket here, no agent binary,
 * somebody turned it off — and each sends the operator somewhere different.
 *
 * `null` when there is nothing to say, which is most of the time: a local
 * agent that is running has a row, and a row says more than a banner.
 * `disabled` is the deliberate exception — it is a choice, not a fault, and
 * worth mentioning only when it is the reason the map is blank.
 */
export function localAgentNotice(
  terms: EnrollmentTerms | null,
  rows: readonly HostRow[],
): string | null {
  if (terms === null) return null;
  // `local`, not `status === 'local'`: a machine that is also enrolled keeps
  // its enrollment status on the row, and it is still being managed locally.
  if (rows.some((row) => row.agent.local)) return null;

  const { state, detail } = terms.local_agent;
  if (state === 'running') return null;
  if (state === 'disabled') {
    return rows.length === 0
      ? 'This machine is not being managed: local_agent.enabled is off. Hosts appear here by enrolling.'
      : null;
  }
  return `This machine is not being managed. ${detail}`;
}

/** Whether two answers from `GET /agents/enrollment` say the same thing. */
export function sameTerms(before: EnrollmentTerms | null, after: EnrollmentTerms): boolean {
  return (
    before !== null &&
    before.enabled === after.enabled &&
    before.auto_approve === after.auto_approve &&
    before.listen === after.listen &&
    before.local_agent.state === after.local_agent.state &&
    before.local_agent.detail === after.local_agent.detail
  );
}

/**
 * Whether two answers from `GET /agents` say the same thing.
 *
 * `last_seen` is excluded on purpose: it moves on every heartbeat of every
 * healthy host, and treating that as news would re-render the app twice a
 * minute per agent to report that nothing happened. It is only ever displayed
 * for hosts that are *not* connected — and losing a connection changes
 * `connected`, which is compared, so the value shown is the one captured at
 * the moment it stopped moving.
 */
export function sameFleet(
  before: readonly EnrolledAgent[],
  after: readonly EnrolledAgent[],
): boolean {
  if (before.length !== after.length) return false;
  return before.every((agent, index) => {
    const other = after[index];
    return (
      agent.engine_id === other.engine_id &&
      agent.status === other.status &&
      agent.connected === other.connected &&
      agent.local === other.local &&
      agent.agent_version === other.agent_version &&
      agent.certificate_expires_at === other.certificate_expires_at
    );
  });
}

// ---------------------------------------------------------------------------
// The join token
// ---------------------------------------------------------------------------

export interface TokenLife {
  readonly remainingMs: number;
  readonly expired: boolean;
  /** `mm:ss` while it is alive, so the countdown reads as one. */
  readonly clock: string;
}

/**
 * How long a minted token has left.
 *
 * Shown as a running clock rather than "expires at 14:32" because the decision
 * it informs is "do I have time to walk to that machine", and a wall-clock
 * time makes the operator do that subtraction themselves. Tokens default to
 * fifteen minutes and are capped at a day (`enrollment.py`), so `mm:ss` is
 * the right unit until the ceiling, where it stops being one and says so.
 */
export function tokenLife(expiresAtUnix: number, now: number): TokenLife {
  const remainingMs = expiresAtUnix * 1000 - now;
  if (remainingMs <= 0) return { remainingMs: 0, expired: true, clock: 'expired' };
  const seconds = Math.floor(remainingMs / 1000);
  if (seconds >= 3600) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return { remainingMs, expired: false, clock: `${hours}h ${minutes}m` };
  }
  const minutes = Math.floor(seconds / 60);
  return {
    remainingMs,
    expired: false,
    clock: `${minutes}:${String(seconds % 60).padStart(2, '0')}`,
  };
}

// ---------------------------------------------------------------------------
// Revocation
// ---------------------------------------------------------------------------

/**
 * The sentence an operator has to agree with before a host is cut off.
 *
 * It names the host, because the panel's rows are near-identical by design and
 * "are you sure?" over a list of hex strings is a coin flip. It also says what
 * revocation does *not* do — the current stream survives until it drops, since
 * this is an allow-list check at connection time rather than a kill switch
 * (`enrollment.py`) — because an operator revoking during an incident will
 * otherwise read the still-flowing telemetry as a failed click and try again.
 */
export function revokeWarning(row: HostRow): string {
  const streaming = row.link.kind === 'connected'
    ? ' It is connected now, and stays connected until that stream drops.'
    : '';
  return `Revoke ${row.label}? Its certificate stops being accepted on the next connection, and it will need a new join token to come back.${streaming}`;
}

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

/** Coarse and past-tense: "4m ago". Never a precision we do not have. */
export function describeSince(unixSeconds: number, now: number): string | null {
  if (!unixSeconds) return null;
  const elapsed = now - unixSeconds * 1000;
  if (elapsed < 0) return 'just now';
  return `${describeDuration(elapsed)} ago`;
}

export function describeDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}
