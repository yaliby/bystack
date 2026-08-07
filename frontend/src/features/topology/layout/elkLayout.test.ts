import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode } from '../../../api/types';
import { computeElkLayout } from './elkLayout';

function node(partial: Partial<GraphNode> & Pick<GraphNode, 'urn' | 'kind' | 'name'>): GraphNode {
  return {
    source: 'local',
    status: 'running',
    labels: {},
    attrs: {},
    observed_at: 0,
    revision: 'r',
    ...partial,
  };
}

function edge(
  kind: GraphEdge['kind'],
  src: string,
  dst: string,
  attrs: Record<string, unknown> = {},
): GraphEdge {
  return {
    key: `${kind}|${src}|${dst}`,
    kind,
    src,
    dst,
    source: 'local',
    attrs,
  };
}

describe('computeElkLayout', () => {
  it('places stack services inside a group box without overlapping centres', async () => {
    const host = node({ urn: 'bystack:host:e1', kind: 'host', name: 'host' });
    const stack = node({ urn: 'bystack:stack:e1/demo', kind: 'stack', name: 'demo' });
    const web = node({ urn: 'bystack:service:e1/demo/web', kind: 'service', name: 'web' });
    const api = node({ urn: 'bystack:service:e1/demo/api', kind: 'service', name: 'api' });
    const db = node({ urn: 'bystack:service:e1/demo/db', kind: 'service', name: 'db' });

    const nodes = [host, stack, web, api, db];
    const edges = [
      edge('contains', stack.urn, web.urn),
      edge('contains', stack.urn, api.urn),
      edge('contains', stack.urn, db.urn),
      edge('depends_on', web.urn, api.urn),
      edge('depends_on', api.urn, db.urn),
    ];

    const { positions, groupBounds, edgePaths } = await computeElkLayout(nodes, edges);

    expect(positions.has(web.urn)).toBe(true);
    expect(positions.has(api.urn)).toBe(true);
    expect(positions.has(db.urn)).toBe(true);
    expect(positions.has(stack.urn)).toBe(false);

    const box = groupBounds.get(stack.urn);
    expect(box).toBeDefined();
    expect(box!.width).toBeGreaterThan(100);
    expect(box!.height).toBeGreaterThan(40);

    // Services should not share the exact same centre.
    const centres = [web, api, db].map((n) => positions.get(n.urn)!);
    const unique = new Set(centres.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`));
    expect(unique.size).toBe(3);

    // Same-stack edges must produce polylines that meet their endpoints.
    expect(edgePaths.size).toBe(2);
    for (const pts of edgePaths.values()) {
      expect(pts.length).toBeGreaterThanOrEqual(2);
      const start = pts[0];
      const end = pts[pts.length - 1];
      const nearAny = (p: { x: number; y: number }) =>
        centres.some((c) => Math.hypot(c.x - p.x, c.y - p.y) < 120);
      expect(nearAny(start)).toBe(true);
      expect(nearAny(end)).toBe(true);
    }
  });

  it('routes published-port links out of their stack and into the host', async () => {
    // The shape of the `.demo` fixtures: separate compose projects that share
    // nothing but the engine. The published port is the only thing that can
    // connect them, so if it does not survive layout the canvas shows three
    // unrelated islands and a host card with no links at all.
    const host = node({ urn: 'bystack:host:e1', kind: 'host', name: 'laptop' });
    const front = node({ urn: 'bystack:stack:e1/frontend', kind: 'stack', name: 'frontend' });
    const data = node({ urn: 'bystack:stack:e1/data', kind: 'stack', name: 'data' });
    const web = node({ urn: 'bystack:service:e1/frontend/web', kind: 'service', name: 'web' });
    const edge_ = node({ urn: 'bystack:service:e1/frontend/edge', kind: 'service', name: 'edge' });
    const pg = node({ urn: 'bystack:service:e1/data/postgres', kind: 'service', name: 'postgres' });

    const nodes = [host, front, data, web, edge_, pg];
    const edges = [
      edge('contains', front.urn, web.urn),
      edge('contains', front.urn, edge_.urn),
      edge('contains', data.urn, pg.urn),
      edge('depends_on', edge_.urn, web.urn),
      edge('exposed_on', web.urn, host.urn, { published: [18080] }),
      edge('exposed_on', edge_.urn, host.urn, { published: [8443] }),
      edge('exposed_on', pg.urn, host.urn, { published: [15432] }),
    ];

    const { positions, groupBounds, edgePaths } = await computeElkLayout(nodes, edges);

    for (const from of [web, edge_, pg]) {
      const path = edgePaths.get(`exposed_on|${from.urn}|${host.urn}`);
      expect(path, `no route for ${from.name} → host`).toBeDefined();
      expect(path!.length).toBeGreaterThanOrEqual(2);
    }

    // Crossing a group boundary must not pull members out of their frame.
    for (const [stack, members] of [
      [front, [web, edge_]],
      [data, [pg]],
    ] as const) {
      const box = groupBounds.get(stack.urn)!;
      expect(box).toBeDefined();
      for (const member of members) {
        const p = positions.get(member.urn)!;
        expect(p.x).toBeGreaterThan(box.x);
        expect(p.x).toBeLessThan(box.x + box.width);
        expect(p.y).toBeGreaterThan(box.y);
        expect(p.y).toBeLessThan(box.y + box.height);
      }
    }

    // The two stacks must stay apart rather than interleaving now that
    // hierarchy handling lets ELK see through the group frames.
    const frontBox = groupBounds.get(front.urn)!;
    const dataBox = groupBounds.get(data.urn)!;
    const disjoint =
      frontBox.x + frontBox.width <= dataBox.x ||
      dataBox.x + dataBox.width <= frontBox.x ||
      frontBox.y + frontBox.height <= dataBox.y ||
      dataBox.y + dataBox.height <= frontBox.y;
    expect(disjoint).toBe(true);
  });
});
