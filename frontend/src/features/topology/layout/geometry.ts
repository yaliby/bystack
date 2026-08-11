/**
 * Node geometry — card extents for the dock-style renderer.
 */

import type { NodeKind } from '../../../api/types';

export type NodeShape =
  | 'host'
  | 'stack'
  | 'service'
  | 'container'
  | 'network'
  | 'volume'
  | 'image';

const SHAPES: Record<NodeKind, NodeShape> = {
  cluster: 'host',
  host: 'host',
  engine: 'host',
  stack: 'stack',
  service: 'service',
  container: 'container',
  network: 'network',
  volume: 'volume',
  image: 'image',
  // Drawn as containers, deliberately. A watched unit and a watched process
  // are workloads on a host in exactly the sense a container is -- they have a
  // state, they can be started and stopped, and an operator reads them the
  // same way. Giving them a shape of their own would say they are a different
  // *sort* of thing, which is the opposite of the point.
  unit: 'container',
  process: 'container',
};

export function shapeOf(kind: NodeKind): NodeShape {
  return SHAPES[kind] ?? 'container';
}

/**
 * Half-width × half-height in world units (card sizes).
 *
 * Heights are tuned to the card's content block (title + subtitle + optional
 * port chip) so cards read as filled, not as boxes with a hollow bottom.
 */
export const NODE_SIZE: Record<NodeShape, readonly [number, number]> = {
  host: [96, 30],
  stack: [4, 4],
  service: [106, 38],
  container: [106, 38],
  network: [88, 30],
  volume: [98, 32],
  image: [84, 30],
};

export function sizeOf(kind: NodeKind): readonly [number, number] {
  return NODE_SIZE[shapeOf(kind)];
}
