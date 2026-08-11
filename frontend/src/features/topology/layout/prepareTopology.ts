/**
 * The topology view model — the single decision about what the canvas shows.
 *
 * Fold containers into services, drop images and networks, keep volumes only
 * where something mounts them, and keep only edges that define readable
 * structure. Networks are deliberately absent as cards: compose's default
 * network is implied by the stack frame, and drawing four services → one
 * network produces an unreadable rail rather than topology.
 *
 * This ran in two places once — here and inside the ELK layout — and the two
 * copies had already drifted on which edge kinds count as structure. Layout
 * and the status bar now read the same object, so what the canvas draws and
 * what the UI claims it drew cannot disagree.
 */

import type { GraphEdge, GraphNode, Urn } from '../../../api/types';

/**
 * Edges that drive layered topology.
 *
 * `contains` is absent on purpose: stack membership is expressed as ELK
 * compound nesting, so an edge for it would draw a line from a frame to the
 * card already inside that frame.
 *
 * `attached_to` is omitted for the same reason wherever the network sits in
 * the same stack — membership is the group itself.
 *
 * `exposed_on` is the exception that has to cross a group boundary: it ties
 * each stack back to the host it publishes on, and it is the only reason
 * separate compose projects read as one system rather than as loose islands.
 */
const LAYOUT_EDGE_KINDS: ReadonlySet<string> = new Set(['depends_on', 'mounts', 'exposed_on']);

/**
 * Leaf kinds that earn a card once they survive folding and filtering.
 *
 * `unit` and `process` are here on the same terms as `container`, and that is
 * the whole point of the feature rather than a detail of it: a watched service
 * is a workload on a host, it has a state, it can be started and stopped, and
 * an operator reads it the same way. Anything selected is drawn — unlike a
 * volume, there is no "dangling and therefore not topology" case to filter,
 * because somebody chose it by hand (ADR-0016).
 */
const LEAF_KINDS: ReadonlySet<string> = new Set([
  'host',
  'service',
  'container',
  'volume',
  'unit',
  'process',
]);

export interface TopologyGraph {
  /** Stacks that have at least one member, drawn as compound group frames. */
  readonly stacks: readonly GraphNode[];
  /** Stack URN → its members, ordered so `depends_on` reads top→bottom. */
  readonly stackMembers: ReadonlyMap<Urn, readonly GraphNode[]>;
  /** The same membership as URNs, which is what the interactive layout wants. */
  readonly stackMemberUrns: ReadonlyMap<Urn, readonly Urn[]>;
  /** Leaves that belong to no stack — the host badge and unclaimed volumes. */
  readonly freeNodes: readonly GraphNode[];
  /** Layout edges, endpoints already resolved through folding. */
  readonly edges: readonly GraphEdge[];
  /** Containers folded into the service that realizes them. */
  readonly foldedContainers: ReadonlySet<Urn>;
  /** Every URN that gets a position. Nothing outside this set is on screen. */
  readonly placed: ReadonlySet<Urn>;
}

export function prepareTopologyGraph(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): TopologyGraph {
  const byUrn = new Map(nodes.map((n) => [n.urn, n]));

  const foldedContainers = new Set<Urn>();
  const containerToService = new Map<Urn, Urn>();
  for (const edge of edges) {
    if (edge.kind !== 'realized_by') continue;
    foldedContainers.add(edge.dst);
    containerToService.set(edge.dst, edge.src);
  }

  const resolve = (urn: Urn): Urn | null =>
    foldedContainers.has(urn) ? (containerToService.get(urn) ?? null) : urn;

  // --- stack membership -------------------------------------------------
  const allStacks = nodes.filter((n) => n.kind === 'stack');
  const members = new Map<Urn, GraphNode[]>();
  for (const stack of allStacks) members.set(stack.urn, []);

  for (const edge of edges) {
    if (edge.kind !== 'contains') continue;
    const kids = members.get(edge.src);
    const child = byUrn.get(edge.dst);
    if (!kids || !child) continue;
    if (child.kind !== 'service' && child.kind !== 'container') continue;
    if (foldedContainers.has(child.urn)) continue;
    kids.push(child);
  }

  // Pull volumes into the stack that mounts them, so a stack's data sits
  // inside its frame instead of trailing an edge across the whole canvas.
  const claimed = new Set<Urn>();
  for (const kids of members.values()) {
    const reach = new Set(kids.map((k) => k.urn));
    // A mount is recorded against the container, not the service that folds it.
    for (const edge of edges) {
      if (edge.kind === 'realized_by' && reach.has(edge.src)) reach.add(edge.dst);
    }
    for (const edge of edges) {
      if (edge.kind !== 'mounts' || !reach.has(edge.src)) continue;
      const volume = byUrn.get(edge.dst);
      if (volume?.kind !== 'volume' || claimed.has(volume.urn)) continue;
      kids.push(volume);
      claimed.add(volume.urn);
    }
  }

  // Services first, then data, each alphabetical: a stable order is what makes
  // a re-layout of unchanged infrastructure produce an unchanged picture.
  for (const kids of members.values()) kids.sort(compareMembers);

  const inStack = new Set<Urn>();
  for (const kids of members.values()) {
    for (const kid of kids) inStack.add(kid.urn);
  }

  const stacks = allStacks.filter((stack) => (members.get(stack.urn)?.length ?? 0) > 0);
  const stackMembers = new Map<Urn, readonly GraphNode[]>();
  const stackMemberUrns = new Map<Urn, readonly Urn[]>();
  for (const stack of stacks) {
    const kids = members.get(stack.urn) ?? [];
    stackMembers.set(stack.urn, kids);
    stackMemberUrns.set(
      stack.urn,
      kids.map((k) => k.urn),
    );
  }

  // --- free leaves ------------------------------------------------------
  const touchesLayoutEdge = (urn: Urn): boolean =>
    edges.some((edge) => {
      if (!LAYOUT_EDGE_KINDS.has(edge.kind)) return false;
      return resolve(edge.src) === urn || resolve(edge.dst) === urn;
    });

  const freeNodes = nodes.filter((node) => {
    if (!LEAF_KINDS.has(node.kind)) return false;
    if (foldedContainers.has(node.urn)) return false;
    if (inStack.has(node.urn)) return false;
    // A dangling anonymous volume is an empty card, not topology.
    if (node.kind === 'volume') return touchesLayoutEdge(node.urn);
    return true;
  });

  const placed = new Set<Urn>([...inStack, ...freeNodes.map((n) => n.urn)]);

  // --- edges ------------------------------------------------------------
  const layoutEdges: GraphEdge[] = [];
  for (const edge of edges) {
    if (!LAYOUT_EDGE_KINDS.has(edge.kind)) continue;

    const src = resolve(edge.src);
    const dst = resolve(edge.dst);
    if (!src || !dst || src === dst) continue;
    if (!placed.has(src) || !placed.has(dst)) continue;

    layoutEdges.push(
      src === edge.src && dst === edge.dst
        ? edge
        : { ...edge, src, dst, key: `${edge.kind}|${src}|${dst}` },
    );
  }

  return {
    stacks,
    stackMembers,
    stackMemberUrns,
    freeNodes,
    edges: layoutEdges,
    foldedContainers,
    placed,
  };
}

function compareMembers(a: GraphNode, b: GraphNode): number {
  const rank = (kind: string) => (kind === 'service' || kind === 'container' ? 0 : 1);
  return rank(a.kind) - rank(b.kind) || a.name.localeCompare(b.name);
}
