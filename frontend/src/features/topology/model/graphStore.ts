/**
 * Client-side graph state.
 *
 * A pure state machine: no React, no DOM, no network. Every rule about
 * applying snapshots and deltas lives here and is unit-testable in isolation,
 * which matters because the sequencing rules are the part most likely to be
 * got subtly wrong.
 *
 * The core invariant, mirrored from the server: deltas apply in `seq` order,
 * and a gap is unrecoverable. We never guess at missing changes -- we ask for
 * a new snapshot.
 */

import type { DeltaMessage, GraphEdge, GraphNode, SnapshotMessage, Urn } from '../../../api/types';

export interface GraphState {
  readonly seq: number;
  readonly nodes: ReadonlyMap<Urn, GraphNode>;
  readonly edges: ReadonlyMap<string, GraphEdge>;
}

export const EMPTY_GRAPH: GraphState = {
  seq: 0,
  nodes: new Map(),
  edges: new Map(),
};

/** Why a delta could not be applied. */
export type ApplyFailure = 'gap' | 'stale';

export type ApplyResult =
  | { readonly ok: true; readonly state: GraphState; readonly changed: boolean }
  | { readonly ok: false; readonly reason: ApplyFailure };

export function applySnapshot(message: SnapshotMessage): GraphState {
  return {
    seq: message.seq,
    nodes: new Map(message.nodes.map((node) => [node.urn, node])),
    edges: new Map(message.edges.map((edge) => [edge.key, edge])),
  };
}

/**
 * Apply one delta.
 *
 * Returns a failure rather than throwing, because both failure modes are
 * expected during normal operation -- a reconnect replays, a slow tab falls
 * behind -- and the caller's response differs: `stale` is ignored, `gap`
 * triggers a resnapshot.
 */
export function applyDelta(state: GraphState, message: DeltaMessage): ApplyResult {
  if (message.seq <= state.seq) {
    // Already reflected. Happens after a resnapshot, when in-flight deltas
    // the snapshot already contains arrive just behind it.
    return { ok: false, reason: 'stale' };
  }
  if (message.seq !== state.seq + 1) {
    // A hole in the sequence. The missing changes are gone; reconstructing
    // them from what we can see would produce a graph that looks plausible
    // and is wrong, which is worse than admitting the gap.
    return { ok: false, reason: 'gap' };
  }

  const nodes = new Map(state.nodes);
  const edges = new Map(state.edges);

  for (const node of message.upserted_nodes) nodes.set(node.urn, node);
  for (const urn of message.removed_nodes) nodes.delete(urn);
  for (const edge of message.upserted_edges) edges.set(edge.key, edge);
  for (const key of message.removed_edges) edges.delete(key);

  // Removing a node orphans its edges. The server sends those removals
  // explicitly, but a client that trusted it blindly would render dangling
  // references the moment the two disagreed.
  for (const [key, edge] of edges) {
    if (!nodes.has(edge.src) || !nodes.has(edge.dst)) edges.delete(key);
  }

  const changed =
    message.upserted_nodes.length > 0 ||
    message.removed_nodes.length > 0 ||
    message.upserted_edges.length > 0 ||
    message.removed_edges.length > 0;

  return { ok: true, state: { seq: message.seq, nodes, edges }, changed };
}

/** Edges touching `urn`, in either direction. Powers relationship tracing. */
export function neighborsOf(state: GraphState, urn: Urn): GraphEdge[] {
  const found: GraphEdge[] = [];
  for (const edge of state.edges.values()) {
    if (edge.src === urn || edge.dst === urn) found.push(edge);
  }
  return found;
}

/** URNs reachable from `urn` within `depth` hops, including `urn` itself. */
export function traceFrom(state: GraphState, urn: Urn, depth = 1): Set<Urn> {
  const reached = new Set<Urn>([urn]);
  let frontier: Urn[] = [urn];

  for (let hop = 0; hop < depth; hop += 1) {
    const next: Urn[] = [];
    for (const current of frontier) {
      for (const edge of neighborsOf(state, current)) {
        const other = edge.src === current ? edge.dst : edge.src;
        if (!reached.has(other)) {
          reached.add(other);
          next.push(other);
        }
      }
    }
    frontier = next;
  }
  return reached;
}
