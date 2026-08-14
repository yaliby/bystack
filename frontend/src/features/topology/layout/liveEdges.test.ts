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

  it('leaves a stack toward the host instead of threading the assembly', () => {
    const host = 'urn:host';
    const prom = 'urn:prom';
    const frame: RouteObstacle = {
      id: 'frame:monitoring',
      x: 0,
      y: 0,
      hw: 200,
      hh: 160,
    };
    const paths = routeDrawnEdges(
      [{ key: 'pub', kind: 'exposed_on', src: prom, dst: host }],
      new Map([
        [prom, { x: 80, y: 100 }],
        [host, { x: 420, y: -40 }],
      ]),
      new Set(['exposed_on']),
      [frame, { id: prom, x: 80, y: 100, hw: 50, hh: 30 }, { id: host, x: 420, y: -40, hw: 90, hh: 30 }],
    );
    const path = paths.get('pub')!;
    // VHV would climb through the stack; the first real step must go toward the host.
    const from = path[0];
    const step = path.find((p) => Math.abs(p.x - from.x) > 8)!;
    expect(step.x).toBeGreaterThan(from.x);

    const right = frame.x + frame.hw;
    let exited = false;
    for (const p of path) {
      if (p.x > right) exited = true;
      if (!exited) continue;
      const inside =
        p.x < right &&
        p.x > frame.x - frame.hw &&
        p.y > frame.y - frame.hh &&
        p.y < frame.y + frame.hh;
      expect(inside).toBe(false);
    }
    expect(exited).toBe(true);
  });

  it('leaves the stack square to the face, level with the card it comes from', () => {
    const host = 'urn:host';
    const svc = 'urn:svc';
    const frame: RouteObstacle = { id: 'frame:stack', x: 0, y: 0, hw: 200, hh: 160 };
    const paths = routeDrawnEdges(
      [{ key: 'pub', kind: 'exposed_on', src: svc, dst: host }],
      new Map([
        [svc, { x: -80, y: 100 }],
        [host, { x: 620, y: -40 }],
      ]),
      new Set(['exposed_on']),
      [frame, { id: svc, x: -80, y: 100, hw: 50, hh: 30 }, { id: host, x: 620, y: -40, hw: 90, hh: 30 }],
    );
    const path = paths.get('pub')!;

    // One segment steps over the right border, and it does it horizontally — a
    // gate is a socket, not a gap the wire happens to find. The socket is cut
    // opposite the card that uses it, so the wire reaches it without a dog-leg
    // inside the stack it is leaving.
    const crossings = path.filter((p, i) => i > 0 && path[i - 1].x < 200 && p.x >= 200);
    expect(crossings).toHaveLength(1);
    const step = path[path.indexOf(crossings[0]) - 1];
    expect(step.y).toBe(100);
    expect(crossings[0].y).toBe(100);
  });

  it('lands links on the host in the order their gates leave the stack', () => {
    const host = 'urn:host';
    const frame: RouteObstacle = { id: 'frame:stack', x: 0, y: 0, hw: 200, hh: 160 };
    // Three services inside one stack, all published on one host to the right.
    // The card order inside says nothing about where the wires come out — the
    // gates do — so the host has to stack its arrivals by gate, or the three
    // wires cross each other in the gap.
    const cards = [
      { urn: 'urn:a', y: 90 },
      { urn: 'urn:b', y: -100 },
      { urn: 'urn:c', y: 10 },
    ];
    const anchors = new Map<string, { x: number; y: number }>([[host, { x: 600, y: 0 }]]);
    const obstacles: RouteObstacle[] = [frame, { id: host, x: 600, y: 0, hw: 90, hh: 30 }];
    for (const card of cards) {
      anchors.set(card.urn, { x: -60, y: card.y });
      obstacles.push({ id: card.urn, x: -60, y: card.y, hw: 50, hh: 30 });
    }

    const paths = routeDrawnEdges(
      cards.map((card) => ({ key: card.urn, kind: 'exposed_on', src: card.urn, dst: host })),
      anchors,
      new Set(['exposed_on']),
      obstacles,
    );

    const landings = cards.map((card) => {
      const path = paths.get(card.urn)!;
      const gate = path.find((p) => p.x >= 200)!;
      return { gate: gate.y, lands: path[path.length - 2].y };
    });

    const byGate = [...landings].sort((a, b) => a.gate - b.gate).map((l) => l.lands);
    expect(byGate).toEqual([...byGate].sort((a, b) => a - b));
    // And they really are three separate arrivals, not one trunk.
    expect(new Set(byGate).size).toBe(3);
  });

  it('fans a bundle out past the gate it all leaves by', () => {
    const host = 'urn:host';
    const frame: RouteObstacle = { id: 'frame:stack', x: 0, y: 0, hw: 200, hh: 160 };
    // Three services in one stack, published on a host that starts beside the
    // stack and is dragged up over it. The wires keep the gates they left by, so
    // all three cross the right face and come at the host's underside from the
    // same column. The columns in front of that face are no use then — each
    // wire's run out to its gate sweeps across them — so the fan files out past
    // the exit instead, each wire turning up on its own column.
    const cards = [
      { urn: 'urn:a', y: 90 },
      { urn: 'urn:b', y: -100 },
      { urn: 'urn:c', y: 10 },
    ];
    const edges = cards.map((card) => ({
      key: card.urn,
      kind: 'exposed_on',
      src: card.urn,
      dst: host,
    }));

    let paths = new Map<string, { x: number; y: number }[]>();
    for (let step = 0; step <= 20; step += 1) {
      const at = { x: 520 - step * 16, y: -step * 21 };
      const anchors = new Map<string, { x: number; y: number }>([[host, at]]);
      const obstacles: RouteObstacle[] = [frame, { id: host, ...at, hw: 96, hh: 30 }];
      for (const card of cards) {
        anchors.set(card.urn, { x: -60, y: card.y });
        obstacles.push({ id: card.urn, x: -60, y: card.y, hw: 50, hh: 30 });
      }
      paths = routeDrawnEdges(edges, anchors, new Set(['exposed_on']), obstacles);
    }

    // Each wire turns up on its own column, and the nearer the wire comes from,
    // the sooner it turns: any other order and a wire still coming has to cross
    // the column of one that has already turned.
    const runs = cards.map((card) => {
      const path = paths.get(card.urn)!;
      return { from: path[0].y, column: path[path.length - 2].x };
    });
    const byNearest = [...runs].sort((a, b) => a.from - b.from).map((run) => run.column);
    expect(byNearest).toEqual([...byNearest].sort((a, b) => a - b));
    for (let i = 1; i < byNearest.length; i += 1) {
      expect(byNearest[i] - byNearest[i - 1]).toBeGreaterThanOrEqual(12);
    }
  });

  it('picks the exit side from where the host actually is', () => {
    const host = 'urn:host';
    const svc = 'urn:svc';
    const frame: RouteObstacle = { id: 'frame:stack', x: 0, y: 0, hw: 200, hh: 160 };
    // Same stack, same card — but a host below rather than beside it. The exit
    // has to follow the target, otherwise "leave toward the host" is just a
    // hard-coded preference for going right.
    const paths = routeDrawnEdges(
      [{ key: 'pub', kind: 'exposed_on', src: svc, dst: host }],
      new Map([
        [svc, { x: 80, y: 100 }],
        [host, { x: 140, y: 460 }],
      ]),
      new Set(['exposed_on']),
      [frame, { id: svc, x: 80, y: 100, hw: 50, hh: 30 }, { id: host, x: 140, y: 460, hw: 90, hh: 30 }],
    );
    const path = paths.get('pub')!;
    const from = path[0];
    const step = path.find((p) => Math.abs(p.y - from.y) > 8)!;
    expect(step.y).toBeGreaterThan(from.y);
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
