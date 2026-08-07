import { beforeEach, describe, expect, it } from 'vitest';
import {
  clearStickyRoutes,
  fanOrtho,
  pathHitsObstacles,
  routeDrawnEdges,
  routeOrthoAvoiding,
  type RouteObstacle,
} from './liveEdges';

describe('routeDrawnEdges', () => {
  beforeEach(() => {
    clearStickyRoutes();
  });

  it('fans links that share a host so trunks are not stacked', () => {
    const host = 'urn:host';
    const anchors = new Map([
      [host, { x: 0, y: 0 }],
      ['a', { x: -120, y: 200 }],
      ['b', { x: 0, y: 200 }],
      ['c', { x: 120, y: 200 }],
    ]);

    const paths = routeDrawnEdges(
      [
        { key: 'e1', kind: 'exposed_on', src: 'a', dst: host },
        { key: 'e2', kind: 'exposed_on', src: 'b', dst: host },
        { key: 'e3', kind: 'exposed_on', src: 'c', dst: host },
      ],
      anchors,
      new Set(['exposed_on']),
    );

    expect(paths.size).toBe(3);

    const approachX = [...paths.values()].map((path) => path[path.length - 2].x);
    const unique = new Set(approachX.map((x) => Math.round(x)));
    expect(unique.size).toBe(3);
  });

  it('holds its corridor across wobble but tracks the live anchors', () => {
    const edges = [{ key: 'link', kind: 'depends_on', src: 'a', dst: 'b' }];
    const kinds = new Set(['depends_on']);
    const first = routeDrawnEdges(
      edges,
      new Map([
        ['a', { x: 0, y: 0 }],
        ['b', { x: 200, y: 40 }],
      ]),
      kinds,
    ).get('link')!;

    const second = routeDrawnEdges(
      edges,
      new Map([
        ['a', { x: 2, y: -1 }],
        ['b', { x: 201, y: 41 }],
      ]),
      kinds,
    ).get('link')!;

    // Same corridor: it did not flip to a different elbow under the wobble.
    expect(second).toHaveLength(first.length);
    // Drawn where the cards are now, not where the quantizer last rounded them.
    expect(second[0]).toEqual({ x: 2, y: -1 });
    expect(second[second.length - 1]).toEqual({ x: 201, y: 41 });
  });

  it('moves a route continuously as an endpoint drifts', () => {
    const edges = [{ key: 'link', kind: 'depends_on', src: 'a', dst: 'b' }];
    const kinds = new Set(['depends_on']);
    let previous: number | null = null;
    let biggestStep = 0;

    // Walk one card a pixel at a time across a full quantizer bucket and then
    // some: every frame must move the wire by about a pixel, never by ten.
    for (let dx = 0; dx <= 40; dx += 1) {
      const path = routeDrawnEdges(
        edges,
        new Map([
          ['a', { x: dx, y: 0 }],
          ['b', { x: 400, y: 260 }],
        ]),
        kinds,
      ).get('link')!;
      const head = path[0].x;
      if (previous !== null) biggestStep = Math.max(biggestStep, Math.abs(head - previous));
      previous = head;
    }

    expect(biggestStep).toBeLessThanOrEqual(1.5);
  });

  it('keeps a lone edge on the direct corridor', () => {
    const path = fanOrtho({ x: 0, y: 0 }, { x: 0, y: 100 }, 0, 1, 0, 1);
    expect(path[0]).toEqual({ x: 0, y: 0 });
    expect(path[path.length - 1]).toEqual({ x: 0, y: 100 });
  });

  it('detours around a card sitting between two endpoints', () => {
    const from = { x: 0, y: 0 };
    const to = { x: 0, y: 300 };
    const wall: RouteObstacle = { id: 'blocker', x: 0, y: 150, hw: 80, hh: 40 };

    const direct = fanOrtho(from, to, 0, 1, 0, 1);
    expect(pathHitsObstacles(direct, [wall])).toBe(true);

    const path = routeOrthoAvoiding(from, to, 0, 1, 0, 1, [wall], new Set());
    expect(pathHitsObstacles(path, [wall])).toBe(false);
    expect(path[0]).toEqual(from);
    expect(path[path.length - 1]).toEqual(to);
  });

  it('ignores the endpoint cards themselves as obstacles', () => {
    const from = { x: 0, y: 0 };
    const to = { x: 200, y: 0 };
    const obstacles: RouteObstacle[] = [
      { id: 'src', x: 0, y: 0, hw: 50, hh: 30 },
      { id: 'dst', x: 200, y: 0, hw: 50, hh: 30 },
    ];
    const path = routeOrthoAvoiding(
      from,
      to,
      0,
      1,
      0,
      1,
      obstacles,
      new Set(['src', 'dst']),
    );
    expect(path[0]).toEqual(from);
    expect(path[path.length - 1]).toEqual(to);
  });
});
