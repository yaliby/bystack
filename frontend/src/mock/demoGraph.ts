/**
 * Mutable demo topology — the discovery half of the mock canvas.
 *
 * Commands in `demoWatch.ts` succeed instantly; the real Controller then waits
 * for the agent to observe the change and push a delta. Without that second
 * half here, Stop leaves the card green and the ActionBar complains that "the
 * topology has not changed" — which is exactly the lie a canned demo must not
 * tell. This file is that second half: a short delay, then a revision bump
 * and a status the operator asked for.
 */

import type { CommandKind, GraphNode, SnapshotMessage, Urn } from '../api/types';
import { DEMO_SNAPSHOT } from './demoSnapshot';

type Listener = (snapshot: SnapshotMessage) => void;

let seq = DEMO_SNAPSHOT.seq;
let nodes: GraphNode[] = DEMO_SNAPSHOT.nodes.map(cloneNode);
const edges = DEMO_SNAPSHOT.edges;
const listeners = new Set<Listener>();

function cloneNode(node: GraphNode): GraphNode {
  return {
    ...node,
    labels: { ...node.labels },
    attrs: { ...node.attrs },
  };
}

function snapshot(): SnapshotMessage {
  return { type: 'snapshot', seq, nodes: nodes.map(cloneNode), edges };
}

function publish(): void {
  const next = snapshot();
  for (const listener of listeners) listener(next);
}

function replace(urn: Urn, next: GraphNode): void {
  const index = nodes.findIndex((node) => node.urn === urn);
  if (index < 0) return;
  nodes = [...nodes.slice(0, index), next, ...nodes.slice(index + 1)];
}

/** Current canned graph. Fresh copy every call — callers must not mutate. */
export function demoGraphSnapshot(): SnapshotMessage {
  return snapshot();
}

export function subscribeDemoGraph(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * How long after a command the "discovery" delta lands.
 *
 * Long enough that `witness` captures the pre-change revision; short enough
 * that the ActionBar does not sit on "waiting for topology" for a breath.
 */
export const DEMO_DISCOVERY_MS = 450;

/** Apply what an agent would have reported after `kind` on `target`. */
export function applyDemoCommand(kind: CommandKind, target: Urn): void {
  const touched = new Set<Urn>();
  expand(kind, target, touched);
  if (touched.size === 0) return;
  seq += 1;
  publish();
}

function expand(kind: CommandKind, target: Urn, touched: Set<Urn>): void {
  if (touched.has(target)) return;
  const node = nodes.find((entry) => entry.urn === target);
  if (!node) return;

  if (node.kind === 'stack') {
    for (const edge of edges) {
      if (edge.kind === 'contains' && edge.src === target) expand(kind, edge.dst, touched);
    }
    return;
  }

  const after = effectOf(kind, node);
  if (after === null) return;
  touched.add(target);
  replace(target, after);

  // Services fold a container: keep the replica in step so the card's LED and
  // the inspector do not disagree after a stop.
  if (node.kind === 'service') {
    const realized = edges.find((edge) => edge.kind === 'realized_by' && edge.src === target);
    if (realized) expand(kind, realized.dst, touched);
  }
}

function effectOf(kind: CommandKind, node: GraphNode): GraphNode | null {
  const revision = `mock-${seq + 1}-${Date.now().toString(36)}`;
  const observed_at = Date.now() / 1000;
  const base = { ...cloneNode(node), revision, observed_at };

  if (node.kind === 'unit') {
    switch (kind) {
      case 'stop':
      case 'kill':
        return {
          ...base,
          status: 'inactive',
          attrs: { ...base.attrs, sub_state: 'dead', main_pid: null },
        };
      case 'start':
      case 'restart':
        return {
          ...base,
          status: 'active',
          attrs: {
            ...base.attrs,
            sub_state: 'running',
            main_pid: typeof base.attrs.main_pid === 'number' ? base.attrs.main_pid : 1000,
            active_since: observed_at,
            load_state: 'loaded',
          },
        };
      default:
        return null;
    }
  }

  if (node.kind === 'process') {
    switch (kind) {
      case 'stop':
      case 'kill':
        return {
          ...base,
          status: 'absent',
          attrs: { ...base.attrs, pids: [], instances: 0, matched: 0 },
        };
      case 'restart':
        // A bare process has no recorded way to be launched — but a restart
        // after stop is what the demo lets you recover with, so bring it back.
        return {
          ...base,
          status: 'running',
          attrs: {
            ...base.attrs,
            pids: [4000 + Math.floor(Math.random() * 500)],
            instances: 1,
            matched: 1,
          },
        };
      default:
        return null;
    }
  }

  if (node.kind === 'service' || node.kind === 'container') {
    switch (kind) {
      case 'stop':
      case 'kill':
        return { ...base, status: 'exited' };
      case 'start':
      case 'restart':
      case 'unpause':
        return { ...base, status: 'running' };
      case 'pause':
        return { ...base, status: 'paused' };
      default:
        return null;
    }
  }

  return null;
}

/** Schedule discovery after a command, matching the live path's lag. */
export function scheduleDemoDiscovery(kind: CommandKind, target: Urn): void {
  window.setTimeout(() => applyDemoCommand(kind, target), DEMO_DISCOVERY_MS);
}

export function scheduleDemoDiscoveryMany(kind: CommandKind, targets: readonly Urn[]): void {
  window.setTimeout(() => {
    for (const target of targets) applyDemoCommand(kind, target);
  }, DEMO_DISCOVERY_MS);
}
