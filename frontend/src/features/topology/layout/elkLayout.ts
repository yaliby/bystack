/**
 * Layered topology layout via ELK — positions + routed edge polylines.
 */

import type { ELK, ElkExtendedEdge, ElkNode } from 'elkjs/lib/elk-api';
import type { GraphEdge, GraphNode, Urn } from '../../../api/types';
import { extractEdgePolylines } from './edgePaths';
import { NODE_SIZE, shapeOf } from './geometry';
import { prepareTopologyGraph, type TopologyGraph } from './prepareTopology';

export interface Point {
  x: number;
  y: number;
}

/**
 * The solver, fetched the first time something needs laying out.
 *
 * ELK is two thirds of the application by weight — a GWT-compiled Java layout
 * engine, and there is no smaller build of it. Loading it up front means the
 * dashboard cannot paint anything until the whole of it has arrived, which is
 * backwards: the first frame is a header, a status bar and an empty canvas,
 * none of which need a solver.
 *
 * Deferring it costs nothing in wall-clock terms because the caller is already
 * `async` and the first layout waits on the graph arriving over the stream.
 * The promise is kept rather than the instance so that two calls that race —
 * which the first snapshot and the first delta routinely do — share one fetch
 * instead of instantiating two engines.
 */
let engine: Promise<ELK> | null = null;

function solver(): Promise<ELK> {
  engine ??= import('elkjs/lib/elk.bundled.js').then((module) => new module.default());
  return engine;
}

const GROUP_PAD = { top: 42, left: 24, bottom: 24, right: 24 };

export interface LayoutSnapshot {
  readonly positions: ReadonlyMap<Urn, Point>;
  readonly groupBounds: ReadonlyMap<Urn, { x: number; y: number; width: number; height: number }>;
  readonly edgePaths: ReadonlyMap<string, Point[]>;
}

export class TopologyLayout {
  private positions = new Map<Urn, Point>();
  private groupBounds = new Map<Urn, { x: number; y: number; width: number; height: number }>();
  private edgePaths = new Map<string, Point[]>();
  private pins = new Map<Urn, Point>();
  private busy = false;
  private generation = 0;
  private fingerprint = '';

  get active(): boolean {
    return this.busy;
  }

  positionsMap(): ReadonlyMap<Urn, Point> {
    return this.positions;
  }

  groups(): ReadonlyMap<Urn, { x: number; y: number; width: number; height: number }> {
    return this.groupBounds;
  }

  paths(): ReadonlyMap<string, Point[]> {
    return this.edgePaths;
  }

  bounds(): { minX: number; minY: number; maxX: number; maxY: number } | null {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    for (const pos of this.positions.values()) {
      minX = Math.min(minX, pos.x - 100);
      minY = Math.min(minY, pos.y - 48);
      maxX = Math.max(maxX, pos.x + 100);
      maxY = Math.max(maxY, pos.y + 48);
    }
    for (const box of this.groupBounds.values()) {
      minX = Math.min(minX, box.x);
      minY = Math.min(minY, box.y);
      maxX = Math.max(maxX, box.x + box.width);
      maxY = Math.max(maxY, box.y + box.height);
    }
    return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
  }

  pin(urn: Urn, at: Point): void {
    this.pins.set(urn, at);
    this.positions.set(urn, at);
  }

  release(urn: Urn): void {
    this.pins.delete(urn);
  }

  async sync(prepared: TopologyGraph): Promise<boolean> {
    const next = fingerprint(prepared);
    if (next === this.fingerprint && this.positions.size > 0) return false;
    this.fingerprint = next;
    this.busy = true;
    const gen = ++this.generation;

    try {
      const result = await layoutPrepared(prepared);
      if (gen !== this.generation) return false;

      const merged = new Map(result.positions);
      for (const [urn, pin] of this.pins) {
        if (merged.has(urn)) merged.set(urn, pin);
      }
      for (const urn of [...this.pins.keys()]) {
        if (!merged.has(urn)) this.pins.delete(urn);
      }

      this.positions = merged;
      this.groupBounds = new Map(result.groupBounds);
      this.edgePaths = new Map(result.edgePaths);
      return true;
    } finally {
      if (gen === this.generation) this.busy = false;
    }
  }
}

/**
 * Identity of a layout input.
 *
 * Only what ELK is actually given: a container restarting changes its status
 * but not the topology, and re-running the solver for that would jump every
 * card on screen for a colour change.
 */
function fingerprint(prepared: TopologyGraph): string {
  const n = [...prepared.placed].sort().join('|');
  const e = prepared.edges
    .map((edge) => edge.key)
    .sort()
    .join('|');
  const g = prepared.stacks
    .map((stack) => `${stack.urn}:${prepared.stackMembers.get(stack.urn)?.length ?? 0}`)
    .sort()
    .join('|');
  return `${n}#${e}#${g}`;
}

export async function computeElkLayout(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): Promise<LayoutSnapshot> {
  return layoutPrepared(prepareTopologyGraph(nodes, edges));
}

/**
 * Lay out an already-prepared topology.
 *
 * What to draw is decided once, in `prepareTopology`; this function only
 * decides where. Keeping the two apart is what lets the status bar count
 * exactly the cards ELK was given.
 */
export async function layoutPrepared(prepared: TopologyGraph): Promise<LayoutSnapshot> {
  const { stacks, stackMembers, freeNodes, edges: layoutEdges } = prepared;

  const children: ElkNode[] = [];

  // Host first so layered DOWN keeps infrastructure above the stack.
  const hostNodes = freeNodes.filter((n) => n.kind === 'host');
  const otherFree = freeNodes.filter((n) => n.kind !== 'host');
  for (const node of hostNodes) children.push(elkLeaf(node));

  for (const stack of stacks) {
    const ordered = stackMembers.get(stack.urn) ?? [];
    if (ordered.length === 0) continue;

    children.push({
      id: stack.urn,
      layoutOptions: {
        'elk.algorithm': 'layered',
        'elk.direction': 'DOWN',
        'elk.edgeRouting': 'ORTHOGONAL',
        'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
        'elk.padding': `[top=${GROUP_PAD.top},left=${GROUP_PAD.left},bottom=${GROUP_PAD.bottom},right=${GROUP_PAD.right}]`,
        'elk.spacing.nodeNode': '34',
        'elk.spacing.edgeEdge': '24',
        'elk.spacing.edgeNode': '24',
        'elk.layered.spacing.nodeNodeBetweenLayers': '52',
        'elk.layered.spacing.edgeEdgeBetweenLayers': '28',
        'elk.layered.spacing.edgeNodeBetweenLayers': '28',
        'elk.layered.unnecessaryBendpoints': 'true',
        'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
        'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
      },
      children: ordered.map((node) => elkLeaf(node)),
    });
  }

  for (const node of otherFree) children.push(elkLeaf(node));

  // Map each leaf → its compound parent (stack), if any. ELK requires edges to
  // live on the lowest common ancestor — same-stack edges on the stack node,
  // cross-stack / free edges on root (DockGraph pattern).
  const leafParent = new Map<Urn, Urn>();
  for (const child of children) {
    if (!child.children?.length) continue;
    for (const leaf of child.children) leafParent.set(leaf.id, child.id);
  }

  const rootEdges: ElkExtendedEdge[] = [];
  const groupEdges = new Map<Urn, ElkExtendedEdge[]>();

  for (const edge of layoutEdges) {
    const elkEdge: ElkExtendedEdge = {
      id: edge.key,
      sources: [edge.src],
      targets: [edge.dst],
    };

    const srcParent = leafParent.get(edge.src);
    const dstParent = leafParent.get(edge.dst);
    if (srcParent && srcParent === dstParent) {
      const list = groupEdges.get(srcParent) ?? [];
      list.push(elkEdge);
      groupEdges.set(srcParent, list);
    } else {
      rootEdges.push(elkEdge);
    }
  }

  for (const child of children) {
    const local = groupEdges.get(child.id);
    if (local?.length) (child as ElkNode & { edges: ElkExtendedEdge[] }).edges = local;
  }

  const graph: ElkNode = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.edgeRouting': 'ORTHOGONAL',
      // Required for `exposed_on`: without it ELK treats each stack as an
      // opaque box and silently drops edges that start inside one and end at
      // the host, which is precisely the link we are here to draw.
      'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
      'elk.separateConnectedComponents': 'true',
      'elk.spacing.nodeNode': '56',
      'elk.spacing.edgeEdge': '24',
      'elk.spacing.edgeNode': '28',
      'elk.spacing.componentComponent': '90',
      // The gap between a stack and the host is not slack — it is the lane the
      // published wires run down and the only place their port chips can be
      // read. Sized for a chip plus air on both sides; at the old 80 there was
      // no room for one, and the labels had to be dumped somewhere arbitrary.
      'elk.layered.spacing.nodeNodeBetweenLayers': '210',
      'elk.layered.spacing.edgeEdgeBetweenLayers': '28',
      'elk.layered.spacing.edgeNodeBetweenLayers': '36',
      'elk.layered.unnecessaryBendpoints': 'true',
      'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
      'elk.padding': '[top=24,left=24,bottom=24,right=24]',
      'elk.aspectRatio': '1.85',
    },
    children,
    edges: rootEdges,
  };

  const laid = await (await solver()).layout(graph);
  const positions = new Map<Urn, Point>();
  const groupBounds = new Map<Urn, { x: number; y: number; width: number; height: number }>();
  const rawPaths = new Map<string, Point[]>();

  collectPositions(laid, 0, 0, positions, groupBounds, new Set(stacks.map((stack) => stack.urn)));
  extractEdgePolylines(laid, 0, 0, rawPaths);

  // Centre the whole figure around the origin.
  let cx = 0;
  let cy = 0;
  if (positions.size > 0) {
    for (const p of positions.values()) {
      cx += p.x;
      cy += p.y;
    }
    cx /= positions.size;
    cy /= positions.size;
  }

  for (const [urn, p] of positions) {
    positions.set(urn, { x: p.x - cx, y: p.y - cy });
  }
  for (const [urn, box] of groupBounds) {
    groupBounds.set(urn, {
      x: box.x - cx,
      y: box.y - cy,
      width: box.width,
      height: box.height,
    });
  }

  // A published port has to leave the stack. Seating those cards on the
  // host-facing column means the wire exits immediately instead of threading
  // the assembly — volumes and un-published siblings sit inward.
  seatPublishedNearHost(positions, groupBounds, prepared);

  const edgePaths = new Map<string, Point[]>();
  for (const [key, pts] of rawPaths) {
    edgePaths.set(
      key,
      pts.map((p) => ({ x: p.x - cx, y: p.y - cy })),
    );
  }

  return { positions, groupBounds, edgePaths };
}

/**
 * Move cards that publish to a host onto the stack column closest to that host.
 *
 * ELK places by internal structure (`depends_on`, `mounts`), which is right
 * for reading the stack — and wrong for the dashed host wires, which then
 * have to walk through a volume or a neighbour to get out. Swapping X only,
 * among the x-slots ELK already chose, keeps everyone inside the frame and
 * leaves vertical order (and the spring-home seats) alone.
 */
function seatPublishedNearHost(
  positions: Map<Urn, Point>,
  groupBounds: ReadonlyMap<Urn, { x: number; y: number; width: number; height: number }>,
  prepared: TopologyGraph,
): void {
  const hosts = prepared.freeNodes.filter((node) => node.kind === 'host' && positions.has(node.urn));
  if (hosts.length === 0) return;

  let hostX = 0;
  for (const host of hosts) hostX += positions.get(host.urn)!.x;
  hostX /= hosts.length;

  const hostUrns = new Set(hosts.map((host) => host.urn));
  const published = new Set<Urn>();
  for (const edge of prepared.edges) {
    if (edge.kind !== 'exposed_on') continue;
    if (hostUrns.has(edge.dst)) published.add(edge.src);
    if (hostUrns.has(edge.src)) published.add(edge.dst);
  }
  if (published.size === 0) return;

  for (const [stackUrn, members] of prepared.stackMembers) {
    const seated = members.filter((member) => positions.has(member.urn));
    if (seated.length < 2) continue;
    const pubs = seated.filter((member) => published.has(member.urn));
    if (pubs.length === 0) continue;

    const box = groupBounds.get(stackUrn);
    const stackCentre =
      box != null
        ? box.x + box.width / 2
        : seated.reduce((sum, member) => sum + positions.get(member.urn)!.x, 0) / seated.length;
    const towardHost = hostX >= stackCentre ? 1 : -1;

    const byY = (a: GraphNode, b: GraphNode) => positions.get(a.urn)!.y - positions.get(b.urn)!.y;
    const inner = seated.filter((member) => !published.has(member.urn));
    const xs = seated.map((member) => positions.get(member.urn)!.x).sort((a, b) => towardHost * (b - a));

    let slot = 0;
    for (const member of [...pubs].sort(byY)) {
      const at = positions.get(member.urn)!;
      positions.set(member.urn, { x: xs[slot], y: at.y });
      slot += 1;
    }
    for (const member of [...inner].sort(byY)) {
      const at = positions.get(member.urn)!;
      positions.set(member.urn, { x: xs[slot], y: at.y });
      slot += 1;
    }
  }
}

function elkLeaf(node: GraphNode): ElkNode {
  const [hw, hh] = NODE_SIZE[shapeOf(node.kind)];
  return {
    id: node.urn,
    width: hw * 2,
    height: hh * 2,
  };
}

function collectPositions(
  node: ElkNode,
  offsetX: number,
  offsetY: number,
  positions: Map<Urn, Point>,
  groupBounds: Map<Urn, { x: number; y: number; width: number; height: number }>,
  stackIds: Set<Urn>,
): void {
  const x = offsetX + (node.x ?? 0);
  const y = offsetY + (node.y ?? 0);

  if (node.id !== 'root' && stackIds.has(node.id)) {
    groupBounds.set(node.id, {
      x,
      y,
      width: node.width ?? 0,
      height: node.height ?? 0,
    });
  } else if (node.id !== 'root' && (!node.children || node.children.length === 0)) {
    const w = node.width ?? 0;
    const h = node.height ?? 0;
    positions.set(node.id, { x: x + w / 2, y: y + h / 2 });
  }

  for (const child of node.children ?? []) {
    collectPositions(child, x, y, positions, groupBounds, stackIds);
  }
}
