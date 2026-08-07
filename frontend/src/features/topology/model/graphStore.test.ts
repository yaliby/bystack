/**
 * The client-side sequencing rules.
 *
 * These are the easiest thing in the frontend to get subtly wrong and the
 * hardest to notice: a mis-applied delta produces a graph that looks
 * plausible and is stale.
 */

import { describe, expect, it } from 'vitest';
import type { DeltaMessage, GraphEdge, GraphNode, SnapshotMessage } from '../../../api/types';
import { EMPTY_GRAPH, applyDelta, applySnapshot, neighborsOf, traceFrom } from './graphStore';

const HOST = 'bystack:host:e1';
const C1 = 'bystack:container:e1/c1';
const C2 = 'bystack:container:e1/c2';

function node(urn: string, name = 'n', status: string | null = null): GraphNode {
  return {
    urn,
    kind: 'container',
    name,
    source: 'docker-a',
    status,
    labels: {},
    attrs: {},
    observed_at: 0,
    revision: `${urn}:${name}:${status}`,
  };
}

function edge(src: string, dst: string): GraphEdge {
  return { key: `hosts|${src}|${dst}`, kind: 'hosts', src, dst, source: 'docker-a', attrs: {} };
}

function snapshot(seq: number, nodes: GraphNode[], edges: GraphEdge[] = []): SnapshotMessage {
  return { type: 'snapshot', seq, nodes, edges };
}

function delta(seq: number, patch: Partial<Omit<DeltaMessage, 'type' | 'seq'>>): DeltaMessage {
  return {
    type: 'delta',
    seq,
    upserted_nodes: [],
    removed_nodes: [],
    upserted_edges: [],
    removed_edges: [],
    ...patch,
  };
}

describe('applySnapshot', () => {
  it('replaces state wholesale', () => {
    const state = applySnapshot(snapshot(7, [node(C1)], [edge(HOST, C1)]));

    expect(state.seq).toBe(7);
    expect(state.nodes.size).toBe(1);
    expect(state.edges.size).toBe(1);
  });
});

describe('applyDelta', () => {
  it('applies the next delta in sequence', () => {
    const state = applySnapshot(snapshot(1, [node(C1)]));
    const result = applyDelta(state, delta(2, { upserted_nodes: [node(C1, 'renamed')] }));

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.state.nodes.get(C1)?.name).toBe('renamed');
    expect(result.state.seq).toBe(2);
  });

  it('ignores a delta the snapshot already contains', () => {
    // Happens after a resnapshot, when deltas still in flight land behind it.
    const state = applySnapshot(snapshot(5, [node(C1)]));
    const result = applyDelta(state, delta(3, { removed_nodes: [C1] }));

    expect(result).toEqual({ ok: false, reason: 'stale' });
  });

  it('refuses to apply across a gap', () => {
    // Reconstructing what we missed would produce a graph that looks right
    // and is wrong. Admitting the gap is the only safe answer.
    const state = applySnapshot(snapshot(1, [node(C1)]));
    const result = applyDelta(state, delta(4, { removed_nodes: [C1] }));

    expect(result).toEqual({ ok: false, reason: 'gap' });
  });

  it('does not mutate the previous state', () => {
    const state = applySnapshot(snapshot(1, [node(C1)]));
    applyDelta(state, delta(2, { removed_nodes: [C1] }));

    expect(state.nodes.has(C1)).toBe(true);
  });

  it('drops edges orphaned by a node removal', () => {
    // Defence in depth: the server sends these removals explicitly, but a
    // client that trusted that blindly would render dangling references the
    // moment the two disagreed.
    const state = applySnapshot(snapshot(1, [node(HOST), node(C1)], [edge(HOST, C1)]));
    const result = applyDelta(state, delta(2, { removed_nodes: [C1] }));

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.state.edges.size).toBe(0);
  });

  it('reports whether anything actually changed', () => {
    const state = applySnapshot(snapshot(1, [node(C1)]));
    const result = applyDelta(state, delta(2, {}));

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.changed).toBe(false);
  });
});

describe('relationship tracing', () => {
  const state = applySnapshot(
    snapshot(1, [node(HOST), node(C1), node(C2)], [edge(HOST, C1), edge(C1, C2)]),
  );

  it('finds edges in both directions', () => {
    expect(neighborsOf(state, C1)).toHaveLength(2);
  });

  it('walks outward by hop count', () => {
    expect(traceFrom(state, HOST, 1)).toEqual(new Set([HOST, C1]));
    expect(traceFrom(state, HOST, 2)).toEqual(new Set([HOST, C1, C2]));
  });

  it('terminates on cycles', () => {
    const cyclic = applySnapshot(
      snapshot(1, [node(C1), node(C2)], [edge(C1, C2), edge(C2, C1)]),
    );

    expect(traceFrom(cyclic, C1, 10)).toEqual(new Set([C1, C2]));
  });
});

describe('EMPTY_GRAPH', () => {
  it('accepts the first delta from a fresh client', () => {
    const result = applyDelta(EMPTY_GRAPH, delta(1, { upserted_nodes: [node(C1)] }));

    expect(result.ok).toBe(true);
  });
});
