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
