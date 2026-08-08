/**
 * Wire types.
 *
 * Mirrors the backend's `api/schemas.py` exactly. These are the only types in
 * the frontend that describe the network; everything downstream works with
 * view models derived from them, so a wire change surfaces here rather than
 * leaking into components.
 */

export type NodeKind =
  | 'cluster'
  | 'host'
  | 'engine'
  | 'stack'
  | 'service'
  | 'container'
  | 'network'
  | 'volume'
  | 'image';

export type EdgeKind =
  | 'hosts'
  | 'contains'
  | 'realized_by'
  | 'attached_to'
  | 'exposed_on'
  | 'mounts'
  | 'uses_image'
  | 'depends_on';

/** A stable entity identifier. See the backend's `core/identity.py`. */
export type Urn = string;

export interface GraphNode {
  readonly urn: Urn;
  readonly kind: NodeKind;
  readonly name: string;
  readonly source: string;
  readonly status: string | null;
  readonly labels: Readonly<Record<string, string>>;
  readonly attrs: Readonly<Record<string, unknown>>;
  readonly observed_at: number;
  readonly revision: string;
}

export interface GraphEdge {
  readonly key: string;
  readonly kind: EdgeKind;
  readonly src: Urn;
  readonly dst: Urn;
  readonly source: string;
  readonly attrs: Readonly<Record<string, unknown>>;
}

export interface SnapshotMessage {
  readonly type: 'snapshot';
  readonly seq: number;
  readonly nodes: readonly GraphNode[];
  readonly edges: readonly GraphEdge[];
}

export interface DeltaMessage {
  readonly type: 'delta';
  readonly seq: number;
  readonly upserted_nodes: readonly GraphNode[];
  readonly removed_nodes: readonly Urn[];
  readonly upserted_edges: readonly GraphEdge[];
  readonly removed_edges: readonly string[];
}

export type StreamMessage = SnapshotMessage | DeltaMessage;

export interface ProviderHealth {
  readonly id: string;
  readonly kind: string;
  readonly state: 'stopped' | 'starting' | 'syncing' | 'ready' | 'degraded' | 'failed';
  readonly detail: string | null;
  readonly last_sync_at: number;
  readonly node_count: number;
  readonly metrics: Readonly<Record<string, string>>;
}

export interface Health {
  readonly status: 'ok' | 'degraded';
  readonly seq: number;
  readonly node_count: number;
  readonly edge_count: number;
  readonly read_only: boolean;
  readonly providers: readonly ProviderHealth[];
}

// ---------------------------------------------------------------------------
// Operations
//
// Mirrors `core/ports/command.py`. Note what is absent: there is no field on
// any of these that a client is meant to apply to its own graph. A command
// reports what the engine did; the topology changes when discovery observes
// it, over the delta stream. See the backend's `runtime/commands.py`.
// ---------------------------------------------------------------------------

export type CommandKind = 'start' | 'stop' | 'restart' | 'pause' | 'unpause' | 'kill';

export type CommandStatus =
  | 'in_flight'
  | 'succeeded'
  /** The target was already in the requested state. Not a failure, not a change. */
  | 'noop'
  | 'failed'
  /** We stopped waiting. The operation may still be completing on the host. */
  | 'timed_out'
  /** Refused before dispatch; the host was never contacted. */
  | 'rejected';

export type RejectionReason =
  | 'read_only'
  | 'unknown_target'
  | 'unsupported_target'
  | 'unsupported_state'
  | 'provider_unavailable'
  | 'too_many_targets';

export interface TargetOutcome {
  readonly target: Urn;
  readonly status: CommandStatus;
  readonly detail: string | null;
  readonly duration_ms: number;
}

export interface CommandResult {
  readonly id: string;
  readonly kind: CommandKind;
  readonly target: Urn;
  readonly status: CommandStatus;
  readonly actor: string;
  readonly requested_at: number;
  readonly duration_ms: number;
  readonly outcomes: readonly TargetOutcome[];
}

/**
 * What the Controller will permit on a node right now.
 *
 * Fetched rather than derived. The rule "unpause applies to paused
 * containers" lives in the provider that owns the state vocabulary; a second
 * copy here would be correct the day it was written and wrong by the release
 * that adds a state.
 */
export interface Actions {
  readonly target: Urn;
  readonly kind: string;
  /** The concrete containers the target expands to. */
  readonly targets: readonly Urn[];
  readonly commands: readonly CommandKind[];
  /** Present when `commands` is empty and there is a reason worth showing. */
  readonly reason: RejectionReason | null;
  readonly detail: string | null;
}

/**
 * One log line, and which stream the container wrote it to.
 *
 * The tag survives the whole way from Docker's multiplexed framing rather
 * than being flattened on the way through, because the line that explains a
 * crash is almost always the one on stderr.
 */
export interface LogLine {
  readonly stderr: boolean;
  readonly text: string;
}

/**
 * The tail of a container's log, or a reason there is none.
 *
 * `ok` is what separates "this container has written nothing" from "we could
 * not ask" — two situations that are the same empty list and nothing like the
 * same answer to whoever is debugging. A disconnected host arrives here as
 * `ok: false` with a reason, not as an HTTP error.
 */
export interface ContainerLogs {
  readonly target: Urn;
  readonly ok: boolean;
  readonly reason: string | null;
  readonly lines: readonly LogLine[];
}

// ---------------------------------------------------------------------------
// Enrollment (ADR-0011)
//
// Mirrors `api/routes/enrollment.py`. A host joins the fleet by redeeming a
// join token for a certificate and then being approved by a person; these are
// the three nouns that describes.
// ---------------------------------------------------------------------------

/**
 * `local` is not an enrollment state and is deliberately spelled apart from
 * the three that are.
 *
 * The Controller spawns an agent for its own machine over a unix socket, with
 * no certificate and no registry entry (`docs/MIGRATION.md` §4). Nobody
 * approved that host and there is nothing to revoke, so folding it into
 * `approved` would put two buttons on a row where neither does anything.
 */
export type AgentStatus = 'pending' | 'approved' | 'revoked' | 'local';

export interface EnrolledAgent {
  /** The Docker Engine ID, which is also the agent's identity and the host's. */
  readonly engine_id: string;
  /** Whether this agent may contribute to the graph. A person decides it. */
  readonly status: AgentStatus;
  readonly certificate_expires_at: number;
  readonly enrolled_at: number;
  readonly last_seen: number;
  readonly agent_version: string;
  /**
   * Whether the agent is on the stream *right now*.
   *
   * Deliberately not part of `status`, and not to be folded into it here
   * either: approved-but-offline and revoked-but-still-streaming are both
   * ordinary states, and each sends an operator somewhere different.
   */
  readonly connected: boolean;

  /**
   * Whether the attached agent is the Controller's own child.
   *
   * A third fact, separate from the other two. A machine can be enrolled *and*
   * currently managed locally — enrol a host, then run the Controller on it —
   * and `status` answers "what did somebody decide about this host's
   * certificate" while this answers "which agent is actually attached". A host
   * with no enrollment behind it at all has `status: 'local'` as well.
   */
  readonly local: boolean;
}

/**
 * A minted join token. Shown once, then gone.
 *
 * The Controller keeps only a digest, so there is nothing to re-read and no
 * route that could return this again. Nothing in the frontend may persist it.
 */
export interface JoinToken {
  readonly token: string;
  readonly expires_at: number;
  readonly ca_fingerprint: string;
  /** The command to paste, composed by the Controller. Never assembled here. */
  readonly install: string;
}

/** Whether a host can join at all, and on what terms. Configuration, not state. */
export interface EnrollmentTerms {
  readonly enabled: boolean;
  readonly auto_approve: boolean;
  readonly listen: string;
  readonly local_agent: LocalAgentStatus;
}

/**
 * Whether this machine is managing itself, and why not when it is not.
 *
 * The explanation for an empty canvas on a first run. Without it, "no Docker
 * socket here" and "the agent binary is missing" and "somebody turned it off"
 * all render as the same nothing.
 */
export interface LocalAgentStatus {
  readonly state: 'running' | 'starting' | 'disabled' | 'unavailable';
  /** Written by the Controller to be shown verbatim. */
  readonly detail: string;
}

export interface AuditEntry {
  readonly id: string;
  readonly at: number;
  readonly actor: string;
  readonly kind: CommandKind;
  readonly target: Urn;
  readonly targets: readonly Urn[];
  readonly status: CommandStatus;
  readonly detail: string | null;
  readonly reason: string | null;
  readonly duration_ms: number;
}
