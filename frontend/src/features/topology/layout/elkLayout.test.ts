import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode } from '../../../api/types';
import { DEMO_SNAPSHOT } from '../../../mock/demoSnapshot';
import { sizeOf } from './geometry';
import { computeElkLayout, layoutPrepared } from './elkLayout';
import {
  clearStickyRoutes,
  pathHitsObstacles,
  routeDrawnEdges,
  type RouteObstacle,
} from './liveEdges';
import { prepareTopologyGraph } from './prepareTopology';

type Side = 'top' | 'right' | 'bottom' | 'left';

function insideFrame(p: { x: number; y: number }, f: RouteObstacle): boolean {
  return Math.abs(p.x - f.x) < f.hw && Math.abs(p.y - f.y) < f.hh;
}

/**
 * Every place the wire steps over the frame's border, with where along that
 * face it did it — an exit through a gate is one crossing, well clear of both
 * corners; a wire that walks the border and turns out at the end is not.
 */
function borderCrossings(
  path: readonly { x: number; y: number }[],
  f: RouteObstacle,
): { face: Side; along: number }[] {
  const out: { face: Side; along: number }[] = [];
  for (let i = 1; i < path.length; i += 1) {
    const a = path[i - 1];
    const b = path[i];
    if (insideFrame(a, f) === insideFrame(b, f)) continue;
    if (Math.abs(a.x - b.x) < 0.5) {
      out.push({
        face: b.y > a.y ? 'bottom' : 'top',
        along: (a.x - (f.x - f.hw)) / (f.hw * 2),
      });
    } else {
      out.push({
        face: b.x > a.x ? 'right' : 'left',
        along: (a.y - (f.y - f.hh)) / (f.hh * 2),
      });
    }
  }
  return out;
}

/** The one face of the frame that points away from a target. */
function awayFace(f: RouteObstacle, target: { x: number; y: number }): Side {
  const dx = target.x - f.x;
  const dy = target.y - f.y;
  if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'left' : 'right';
  return dy >= 0 ? 'top' : 'bottom';
}

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

  it('seats a published service on the host-facing side of its stack', async () => {
    const host = node({ urn: 'bystack:host:e1', kind: 'host', name: 'laptop' });
    const stack = node({ urn: 'bystack:stack:e1/mon', kind: 'stack', name: 'monitoring' });
    const prom = node({ urn: 'bystack:service:e1/mon/prometheus', kind: 'service', name: 'prometheus' });
    const vol = node({ urn: 'bystack:volume:e1/data', kind: 'volume', name: 'data' });

    const { positions } = await computeElkLayout(
      [host, stack, prom, vol],
      [
        edge('contains', stack.urn, prom.urn),
        edge('mounts', prom.urn, vol.urn),
        edge('exposed_on', prom.urn, host.urn, { published: [9090] }),
      ],
    );

    const hostX = positions.get(host.urn)!.x;
    const promX = positions.get(prom.urn)!.x;
    const volX = positions.get(vol.urn)!.x;
    // The published card sits closer to the host than the volume it mounts.
    expect(Math.abs(promX - hostX)).toBeLessThan(Math.abs(volX - hostX));
  });

  it('host wires leave the stack through a gate on a host-facing side', async () => {
    clearStickyRoutes();
    const prepared = prepareTopologyGraph([...DEMO_SNAPSHOT.nodes], [...DEMO_SNAPSHOT.edges]);
    const laid = await layoutPrepared(prepared);

    const obstacles: RouteObstacle[] = [];
    for (const n of DEMO_SNAPSHOT.nodes) {
      if (n.kind === 'stack' || n.kind === 'image' || n.kind === 'network') continue;
      if (prepared.foldedContainers.has(n.urn)) continue;
      const p = laid.positions.get(n.urn);
      if (!p) continue;
      const [hw, hh] = sizeOf(n.kind);
      obstacles.push({ id: n.urn, x: p.x, y: p.y, hw, hh });
    }
    for (const [urn, box] of laid.groupBounds) {
      obstacles.push({
        id: `frame:${urn}`,
        x: box.x + box.width / 2,
        y: box.y + box.height / 2,
        hw: box.width / 2,
        hh: box.height / 2,
      });
    }

    const host = [...laid.positions.keys()].find((urn) => urn.includes(':host:'))!;
    const hostAt = laid.positions.get(host)!;
    const edges = prepared.edges.filter((e) => e.kind === 'exposed_on');
    const routes = routeDrawnEdges(edges, laid.positions, new Set(['exposed_on']), obstacles);

    for (const edge of edges) {
      const from = laid.positions.get(edge.src)!;
      const path = routes.get(edge.key)!;
      const sourceFrame = obstacles.find(
        (o) =>
          o.id.startsWith('frame:') &&
          Math.abs(from.x - o.x) < o.hw &&
          Math.abs(from.y - o.y) < o.hh,
      );
      expect(sourceFrame, `no stack frame for ${edge.src}`).toBeDefined();

      // Neighbour assemblies are solid — only the source frame may be crossed.
      const foreign = obstacles.filter((o) => o.id.startsWith('frame:') && o.id !== sourceFrame!.id);
      expect(pathHitsObstacles(path, foreign)).toBe(false);

      // Out through one gate: the border is crossed once, square to the face,
      // and the wire never comes back inside.
      const crossings = borderCrossings(path, sourceFrame!);
      expect(crossings, `no clean exit for ${edge.src}`).toHaveLength(1);
      // Through the face, not squeezed past a corner of it.
      expect(crossings[0].along).toBeGreaterThan(0.1);
      expect(crossings[0].along).toBeLessThan(0.9);

      // The face it leaves by is one that faces the host — never the one
      // pointing away from it.
      expect(crossings[0].face).not.toBe(awayFace(sourceFrame!, hostAt));
    }
  });
});
