import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode } from '../../../api/types';
import { isCanvasEdge, prepareTopologyGraph } from './prepareTopology';

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

function edge(kind: GraphEdge['kind'], src: string, dst: string): GraphEdge {
  return { key: `${kind}|${src}|${dst}`, kind, src, dst, source: 'local', attrs: {} };
}

/** The `.demo` compose stack: web → api → db, plus a volume and a host port. */
function demo() {
  const host = node({ urn: 'bystack:host:e1', kind: 'host', name: 'laptop' });
  const stack = node({ urn: 'bystack:stack:e1/demo', kind: 'stack', name: 'demo' });
  const web = node({ urn: 'bystack:service:e1/demo/web', kind: 'service', name: 'web' });
  const db = node({ urn: 'bystack:service:e1/demo/db', kind: 'service', name: 'db' });
  const webC = node({ urn: 'bystack:container:e1/c-web', kind: 'container', name: 'demo-web-1' });
  const dbC = node({ urn: 'bystack:container:e1/c-db', kind: 'container', name: 'demo-db-1' });
  const vol = node({ urn: 'bystack:volume:e1/pgdata', kind: 'volume', name: 'pgdata' });
  const net = node({ urn: 'bystack:network:e1/demo_default', kind: 'network', name: 'demo_default' });
  const img = node({ urn: 'bystack:image:sha256-a', kind: 'image', name: 'nginx:alpine' });

  const nodes = [host, stack, web, db, webC, dbC, vol, net, img];
  const edges = [
    edge('contains', stack.urn, web.urn),
    edge('contains', stack.urn, db.urn),
    edge('realized_by', web.urn, webC.urn),
    edge('realized_by', db.urn, dbC.urn),
    edge('depends_on', web.urn, db.urn),
    edge('mounts', dbC.urn, vol.urn),
    edge('exposed_on', web.urn, host.urn),
    edge('attached_to', webC.urn, net.urn),
    edge('uses_image', webC.urn, img.urn),
  ];
  return { host, stack, web, db, webC, dbC, vol, net, img, nodes, edges };
}

describe('prepareTopologyGraph', () => {
  it('folds containers into the services that realize them', () => {
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, f.edges);

    expect(prepared.foldedContainers.has(f.webC.urn)).toBe(true);
    expect(prepared.placed.has(f.webC.urn)).toBe(false);
    expect(prepared.placed.has(f.web.urn)).toBe(true);
  });

  it('never places images or networks', () => {
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, f.edges);

    expect(prepared.placed.has(f.net.urn)).toBe(false);
    expect(prepared.placed.has(f.img.urn)).toBe(false);
  });

  it('claims a mounted volume into the stack that mounts it', () => {
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, f.edges);

    const members = prepared.stackMembers.get(f.stack.urn)!;
    expect(members.map((m) => m.urn)).toContain(f.vol.urn);
    // Services before data, so `depends_on` reads top→bottom inside the frame.
    expect(members[members.length - 1].urn).toBe(f.vol.urn);
  });

  it('drops a volume nothing mounts', () => {
    const f = demo();
    const orphan = node({ urn: 'bystack:volume:e1/orphan', kind: 'volume', name: 'orphan' });
    const prepared = prepareTopologyGraph([...f.nodes, orphan], f.edges);

    expect(prepared.placed.has(orphan.urn)).toBe(false);
  });

  it('rewrites a folded endpoint onto its service', () => {
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, f.edges);

    // `mounts` was recorded against the container; the card is the service.
    const mount = prepared.edges.find((e) => e.kind === 'mounts')!;
    expect(mount.src).toBe(f.db.urn);
    expect(mount.dst).toBe(f.vol.urn);
    expect(mount.key).toBe(`mounts|${f.db.urn}|${f.vol.urn}`);
  });

  it('keeps only edges that define structure', () => {
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, f.edges);
    const kinds = new Set(prepared.edges.map((e) => e.kind));

    expect(kinds).toEqual(new Set(['depends_on', 'mounts', 'exposed_on']));
    // `contains` is the group frame, not a line drawn to what is inside it.
    expect(kinds.has('contains')).toBe(false);
  });

  it('is the whole truth about what is on screen', () => {
    // The invariant the status bar depends on: every endpoint of every layout
    // edge is a node that got placed. A counter derived from `placed` can
    // therefore never name something the canvas did not draw.
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, f.edges);

    for (const layoutEdge of prepared.edges) {
      expect(prepared.placed.has(layoutEdge.src)).toBe(true);
      expect(prepared.placed.has(layoutEdge.dst)).toBe(true);
    }

    const drawn = [...prepared.freeNodes, ...[...prepared.stackMembers.values()].flat()];
    expect(new Set(drawn.map((n) => n.urn))).toEqual(prepared.placed);
  });

  it('hides a stack that has no members left to draw', () => {
    const empty = node({ urn: 'bystack:stack:e1/ghost', kind: 'stack', name: 'ghost' });
    const prepared = prepareTopologyGraph([empty], []);

    expect(prepared.stacks).toHaveLength(0);
    expect(prepared.placed.size).toBe(0);
  });

  it('anchors a watched unit and process to the host that runs them', () => {
    // Without this the pair joins no stack and touches no other layout edge, so
    // each one lays out as its own free component and the canvas never says
    // which machine it belongs to.
    const f = demo();
    const unit = node({ urn: 'bystack:unit:e1/nginx', kind: 'unit', name: 'nginx.service' });
    const proc = node({ urn: 'bystack:process:e1/4021', kind: 'process', name: 'worker' });
    const prepared = prepareTopologyGraph(
      [...f.nodes, unit, proc],
      [...f.edges, edge('hosts', f.host.urn, unit.urn), edge('hosts', f.host.urn, proc.urn)],
    );

    expect(prepared.placed.has(unit.urn)).toBe(true);
    expect(prepared.placed.has(proc.urn)).toBe(true);
    for (const dst of [unit.urn, proc.urn]) {
      expect(prepared.edges).toContainEqual(
        expect.objectContaining({ kind: 'hosts', src: f.host.urn, dst }),
      );
    }
  });

  it('treats host→unit as a canvas edge and host→container as not', () => {
    // Drawn and laid out by the same rule: the wire an operator expects when
    // watching nginx on lowserv, and never a rail from every container.
    const kinds = new Map<string, string>([
      ['bystack:host:e1', 'host'],
      ['bystack:unit:e1/nginx', 'unit'],
      ['bystack:container:e1/c', 'container'],
    ]);
    const kindOf = (urn: string) => kinds.get(urn);
    expect(
      isCanvasEdge(edge('hosts', 'bystack:host:e1', 'bystack:unit:e1/nginx'), kindOf),
    ).toBe(true);
    expect(
      isCanvasEdge(edge('hosts', 'bystack:host:e1', 'bystack:container:e1/c'), kindOf),
    ).toBe(false);
  });

  it('does not draw a rail from the host to every container it hosts', () => {
    // Providers emit `hosts` for containers, stacks, networks and volumes too.
    // Taking the kind wholesale would put one line per container into the host
    // badge, which is the picture this module exists to prevent.
    const f = demo();
    const prepared = prepareTopologyGraph(f.nodes, [
      ...f.edges,
      edge('hosts', f.host.urn, f.webC.urn),
      edge('hosts', f.host.urn, f.dbC.urn),
      edge('hosts', f.host.urn, f.stack.urn),
      edge('hosts', f.host.urn, f.vol.urn),
      edge('hosts', f.host.urn, f.net.urn),
    ]);

    expect(prepared.edges.filter((e) => e.kind === 'hosts')).toHaveLength(0);
  });

  it('does not let a hosts edge alone keep a dangling volume on the canvas', () => {
    // A volume earns a card by being mounted. `hosts` reaches every volume on
    // the machine, so it must not count as the edge that rescues one.
    const host = node({ urn: 'bystack:host:e1', kind: 'host', name: 'laptop' });
    const orphan = node({ urn: 'bystack:volume:e1/stray', kind: 'volume', name: 'stray' });
    const prepared = prepareTopologyGraph(
      [host, orphan],
      [edge('hosts', host.urn, orphan.urn)],
    );

    expect(prepared.placed.has(orphan.urn)).toBe(false);
  });

  it('survives an empty graph', () => {
    const prepared = prepareTopologyGraph([], []);

    expect(prepared.placed.size).toBe(0);
    expect(prepared.edges).toHaveLength(0);
    expect(prepared.stacks).toHaveLength(0);
  });
});
