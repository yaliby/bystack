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
  | 'image'
  /** A systemd unit the operator selected. See `providers/host/mapper.py`. */
  | 'unit'
  /** A process watch rule — the rule, never the pid. Same file. */
  | 'process';

export type EdgeKind =
  | 'hosts'
  | 'contains'
  | 'realized_by'
  | 'attached_to'
  | 'exposed_on'
  | 'mounts'
  | 'uses_image'
  | 'depends_on'
  /** process → the container or unit whose cgroup it is in. */
  | 'runs_in';

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
  /** What the Controller is. Only meaningful next to an agent's own version. */
  readonly version: string;
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
  /** The same, for a host that already has the binary. Also the Controller's. */
  readonly manual: string;
}

/** Whether a host can join at all, and on what terms. Configuration, not state. */
export interface EnrollmentTerms {
  readonly enabled: boolean;
  readonly auto_approve: boolean;
  readonly listen: string;
  /**
   * What to run on a host whose agent is older than the Controller.
   *
   * Composed by the Controller, so it carries the running version and the
   * address agents dial — and deliberately carries no token: one beside a
   * stored certificate makes the agent enrol again, and the host returns as a
   * stranger awaiting approval while the one you have goes quiet. Shown
   * verbatim; the UI never assembles this from parts.
   */
  readonly upgrade: string;
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

/**
 * A signed agent release this Controller can hand out (ADR-0017).
 *
 * The Controller holds no signing key and cannot make one of these. It picks
 * the artifact matching each host and sends it; the host decides whether to
 * run it, against a key compiled into the agent. So this is a *distribution*
 * list, not an authority — which is why nothing in the UI presents it as
 * "trusted" anything.
 */
export interface AgentRelease {
  readonly version: string;
  readonly arch: string;
  readonly sha256: string;
  readonly released_at: number;
  readonly size: number;
}

/** What can be pushed, and who would take it. */
export interface AgentReleases {
  /** Where the Controller looks. Named because the empty case is the common one. */
  readonly directory: string;
  readonly releases: readonly AgentRelease[];
  /** Hosts that could be sent a release right now. */
  readonly upgradable: readonly string[];
  /**
   * Connected hosts that cannot take one.
   *
   * An agent from before this feature, or one built with no signing keys.
   * Named rather than left out of the count silently: a rollout steps over
   * these, and an operator who cannot see which they are has no way to know
   * why four machines were left behind.
   */
  readonly stale: readonly string[];
}

/** What one host did with one release. */
export interface RolloutResult {
  readonly engine_id: string;
  /** `confirmed`, `refused`, `failed` or `skipped`. */
  readonly state: string;
  readonly reason: string | null;
  readonly version: string;
}

/**
 * A staged rollout, running or finished.
 *
 * One host at a time, confirmed by that host's next `Hello`, and a failure
 * stops the run rather than completing it. There is no progress model here
 * beyond `current`: the version per host is already on `GET /agents` and
 * already drawn, so a run's progress is the fleet's own state changing.
 */
export interface Rollout {
  readonly version: string;
  /** `running`, `finished`, `stopped` (by an operator) or `failed` (by a host). */
  readonly state: string;
  readonly planned: readonly string[];
  readonly current: string | null;
  readonly detail: string | null;
  readonly started_at: number;
  readonly finished_at: number;
  readonly results: readonly RolloutResult[];
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

// --------------------------------------------------------------------------
// Watched units and processes
//
// The one part of this API that is *configuration* rather than observation.
// Everything else here describes what the fleet reported; these describe what
// the operator asked to be shown, which is the thing nothing out there knows
// (the backend's `core/ports/watch.py` says why that has to be durable).
// --------------------------------------------------------------------------

export type WatchKind = 'unit' | 'process';

/**
 * How a process rule decides what it is looking at.
 *
 * Substring and equality only. There is deliberately no regex option: the
 * pattern is evaluated inside a process on a machine we do not own, against
 * every entry in /proc.
 */
export type MatchKind = 'name' | 'exec' | 'cmdline';

export interface WatchEntry {
  readonly id: string;
  readonly engine_id: string;
  readonly kind: WatchKind;
  /**
   * Which act of selection this entry came from.
   *
   * Shared by everything chosen in one go, and by nothing else. It carries no
   * authority: these entries are independent, and stopping one does not touch
   * the others. It is here so the panel can say where a row came from.
   */
  readonly group_id: string;
  /**
   * How many hosts hold an entry from that same act of selection, this one
   * included. `1` means it was chosen for this host alone.
   */
  readonly group_hosts: number;
  readonly name: string;
  readonly match_kind: MatchKind | null;
  readonly pattern: string;
  readonly label: string;
  readonly added_at: number;
  /** The node this becomes, whether or not it exists on the host yet. */
  readonly urn: Urn;
}

export interface WatchList {
  readonly engine_id: string;
  readonly entries: readonly WatchEntry[];
  /**
   * Whether the host currently holds this list.
   *
   * `false` is not an error: the list is the Controller's and it is durable,
   * so a machine that is asleep learns about the edit when it reconnects. It
   * is shown so an operator who just added a service knows why no card has
   * appeared yet.
   */
  readonly delivered: boolean;
  readonly detail: string | null;
}

/** What one host made of a fan-out. */
export interface FanoutHost {
  readonly engine_id: string;
  readonly stored: boolean;
  readonly delivered: boolean;
  /**
   * Why it was not stored, or why the host has not been told. The
   * Controller's own words, meant to be shown.
   */
  readonly detail: string | null;
}

/**
 * The outcome of watching one thing across several hosts.
 *
 * Per host and never a single verdict, because the failures are partial by
 * nature: eight machines storing the entry and one already holding it is not
 * an error, and it is not a clean success either.
 */
export interface WatchFanout {
  readonly group_id: string;
  readonly stored: number;
  readonly hosts: readonly FanoutHost[];
}

/** What one host made of a command sent to a whole group. */
export interface GroupCommandHost {
  readonly engine_id: string;
  readonly urn: Urn | '';
  /** False for a host that refused before anything was dispatched. */
  readonly ran: boolean;
  readonly status: string;
  readonly detail: string | null;
  readonly result: CommandResult | null;
}

/**
 * One lifecycle command across a chosen set of hosts.
 *
 * `status` is worst-wins across the hosts, the same rule a single command
 * applies across the targets within one host: eight machines restarted and
 * one asleep is a failure, so the operator goes and finds the ninth instead
 * of believing the fleet is consistent.
 */
export interface GroupCommandResult {
  readonly kind: CommandKind;
  readonly group_id: string;
  readonly status: string;
  readonly hosts: readonly GroupCommandHost[];
}

/** One row of the picker: something that *could* be watched. */
export interface InventoryItem {
  /** What a watch entry would name — a unit name, or an executable path. */
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly state: string;
  readonly detail: string;
  readonly pid: number;
}

export interface Inventory {
  readonly engine_id: string;
  readonly kind: WatchKind;
  readonly ok: boolean;
  readonly reason: string | null;
  readonly items: readonly InventoryItem[];
  /**
   * Matches before the agent's cap. Shown as "200 of 412" rather than
   * presenting a truncated list as the whole machine — the operator whose
   * service is number three hundred would otherwise conclude it is not
   * installed.
   */
  readonly total: number;
}

/**
 * How far the local updater has got with replacing this Controller (ADR-0018).
 *
 * Everything here is what a root process wrote to a file. Nothing is held in
 * the Controller's memory, and that is not a limitation being worked around --
 * the middle of a successful update is *the process serving this API being
 * stopped and replaced*, so anything in memory would be lost at the moment
 * somebody is watching it.
 */
export interface ControllerUpdate {
  /**
   * `fetching`, `verifying`, `applying`, `probation`, `cascading`, `success`,
   * `failed` or `rolled_back`.
   *
   * A closed set rather than a percentage. `rolled_back` in particular is not
   * a failure to draw as one: the machine is working, on the version it was.
   */
  readonly phase: string;
  readonly version: string;
  readonly detail: string;
  /** The version the fleet is being rolled to behind this. Empty until there is one. */
  readonly cascade: string;
  readonly updated_at: number;
  readonly running: boolean;
}

/** This Controller, and whether it has a local updater to talk to. */
export interface ControllerSelf {
  readonly version: string;
  readonly updatable: boolean;
  /**
   * Why not, in a sentence, when `updatable` is false.
   *
   * Which is most installs — a container, a checkout, a `pip install`. A panel
   * that silently has no button looks broken; one that says "this is a
   * container, upgrade the image" is documentation where the question is asked.
   */
  readonly reason: string;
  readonly read_only: boolean;
  readonly update: ControllerUpdate | null;
}
