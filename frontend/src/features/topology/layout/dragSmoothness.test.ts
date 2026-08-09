/**
 * Wires must travel with the cards they join, not snap between corridors.
 *
 * `liveEdges.test.ts` checks a route in isolation. This drives a real drag
 * through `InteractiveLayout` and routes off its live positions the way the
 * render loop does, because the flicker only appears in the combination: cards
 * wobbling by a few pixels were enough to make the router re-decide, and a
 * re-decision moves the whole wire at once.
 *
 * The measure is *excess* — how much further a wire moved in one frame than
 * the furthest any card moved. Motion the cards paid for is free; anything on
 * top of it is the wire changing shape under the eye.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import type { GraphNode } from '../../../api/types';
import type { Point } from './elkLayout';
import { sizeOf } from './geometry';
import { InteractiveLayout } from './interactiveLayout';
import { clearStickyRoutes, routeDrawnEdges, type RouteEdge, type RouteObstacle } from './liveEdges';

/** Excess above this reads as a snap rather than as following the cards. */
const SNAP_PX = 20;
const FRAMES = 240;

interface DragReport {
  /** Frames where some wire outran its cards. */
  readonly snaps: number;
  /** Total excess travel, so a fix that only shrinks the jumps still shows. */
  readonly snapPx: number;
  readonly worstExcess: number;
}

function node(urn: string, name: string): GraphNode {
  return {
    urn,
    kind: 'service',
    name,
    source: 'local',
    status: 'running',
    labels: {},
    attrs: {},
    observed_at: 0,
    revision: 'r',
  };
}

function polylineLength(path: readonly Point[]): number {
  let total = 0;
  for (let i = 1; i < path.length; i += 1) {
    total += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
  }
  return total;
}

function pointAt(path: readonly Point[], t: number): Point {
  let remaining = t * polylineLength(path);
  for (let i = 1; i < path.length; i += 1) {
    const segment = Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
    if (segment >= remaining) {
      const k = remaining / (segment || 1);
      return {
        x: path[i - 1].x + (path[i].x - path[i - 1].x) * k,
        y: path[i - 1].y + (path[i].y - path[i - 1].y) * k,
      };
    }
    remaining -= segment;
  }
  return path[path.length - 1];
}

/**
 * Worst displacement of any point along the wire between two frames.
 *
 * Comparing by arc length rather than by vertex handles a corridor that gains
 * or loses a bend, which is exactly the change worth catching.
 */
function frameJump(before: readonly Point[], after: readonly Point[]): number {
  let worst = 0;
  for (let i = 0; i <= 24; i += 1) {
    const a = pointAt(before, i / 24);
    const b = pointAt(after, i / 24);
    worst = Math.max(worst, Math.hypot(a.x - b.x, a.y - b.y));
  }
  return worst;
}

function dragProbe(pointerAt: (t: number) => Point): DragReport {
  const nodes = [
    node('svc:web', 'web'),
    node('svc:api', 'api'),
    node('svc:db', 'db'),
    node('svc:cache', 'cache'),
  ];
  const seats = new Map<string, Point>([
    ['svc:web', { x: -220, y: -120 }],
    ['svc:api', { x: 60, y: -120 }],
    ['svc:db', { x: -220, y: 140 }],
    ['svc:cache', { x: 60, y: 140 }],
  ]);
  // db is a shared endpoint, so the fan slots are exercised too.
  const edges: RouteEdge[] = [
    { key: 'e1', kind: 'depends_on', src: 'svc:web', dst: 'svc:db' },
    { key: 'e2', kind: 'depends_on', src: 'svc:api', dst: 'svc:db' },
    { key: 'e3', kind: 'depends_on', src: 'svc:cache', dst: 'svc:db' },
    { key: 'e4', kind: 'depends_on', src: 'svc:web', dst: 'svc:api' },
  ];
  const kinds = new Set(['depends_on']);

  const layout = new InteractiveLayout();
  layout.seed(
    seats,
    new Map([['stack:demo', { x: -400, y: -300, width: 800, height: 600 }]]),
    new Map(),
    new Map([['stack:demo', [...seats.keys()]]]),
    nodes,
  );
  // Settle first: `seed` scatters new cards, and the drag has to start from
  // rest for the run to be repeatable.
  for (let i = 0; i < 300; i += 1) layout.step();

  const lastPath = new Map<string, Point[]>();
  let lastCards = new Map<string, Point>();
  let snaps = 0;
  let snapPx = 0;
  let worstExcess = 0;

  for (let step = 0; step <= FRAMES; step += 1) {
    layout.move('svc:web', pointerAt(step / FRAMES));
    layout.step();

    const positions = layout.positionsMap();
    const obstacles: RouteObstacle[] = nodes.map((n) => {
      const at = positions.get(n.urn)!;
      const [hw, hh] = sizeOf(n.kind);
      return { id: n.urn, x: at.x, y: at.y, hw, hh };
    });

    let cardStep = 0;
    for (const n of nodes) {
      const now = positions.get(n.urn)!;
      const before = lastCards.get(n.urn);
      if (before) cardStep = Math.max(cardStep, Math.hypot(now.x - before.x, now.y - before.y));
    }
    lastCards = new Map([...positions].map(([urn, at]) => [urn, { ...at }]));

    const routed = routeDrawnEdges(edges, new Map(positions), kinds, obstacles);
    for (const [key, path] of routed) {
      const before = lastPath.get(key);
      if (before) {
        const excess = frameJump(before, path) - cardStep;
        if (excess > SNAP_PX) {
          snaps += 1;
          snapPx += excess;
        }
        worstExcess = Math.max(worstExcess, excess);
      }
      lastPath.set(
        key,
        path.map((p) => ({ ...p })),
      );
    }
  }

  return { snaps, snapPx, worstExcess: Math.round(worstExcess) };
}

describe('wires during a drag', () => {
  beforeEach(() => {
    clearStickyRoutes();
  });

  /**
   * The one that has to be exact. Nothing is crossing anything and no card
   * moves more than 5px in a frame, so there is no corridor decision left to
   * make — every wire is simply following its endpoints. Before the routing
   * held its corridor this run snapped 58 times for 6300px, which is the
   * shimmer you see holding a card still.
   */
  it('never snaps while a card is jiggled in place', () => {
    const report = dragProbe((t) => ({
      x: -220 + Math.sin(t * Math.PI * 30) * 9,
      y: -120 + Math.cos(t * Math.PI * 37) * 7,
    }));
    expect(report).toEqual({ snaps: 0, snapPx: 0, worstExcess: 0 });
  });

  /**
   * Sweeping a card past its neighbours does re-route wires — cards genuinely
   * move into the way, and a link that would be drawn straight through a card
   * has to go around it. The budget is for those, not for the coin-flip
   * between two elbows of equal length that used to sit on top of them.
   */
  it('re-routes rarely while a card is swept across its neighbours', () => {
    const report = dragProbe((t) => ({ x: -300 + t * 620, y: -110 + t * 30 }));
    expect(report.snaps).toBeLessThanOrEqual(15);
  });

  /** Same again for a full circle, which walks the card past every neighbour. */
  it('re-routes rarely while a card is dragged in a circle', () => {
    const report = dragProbe((t) => ({
      x: Math.cos(t * Math.PI * 2) * 240,
      y: Math.sin(t * Math.PI * 2) * 180,
    }));
    expect(report.snaps).toBeLessThanOrEqual(32);
  });
});
