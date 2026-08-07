import { describe, expect, it } from 'vitest';
import type { GraphNode } from '../../../api/types';
import { InteractiveLayout } from './interactiveLayout';

function node(urn: string, kind: GraphNode['kind'], name: string): GraphNode {
  return {
    urn,
    kind,
    name,
    source: 'local',
    status: 'running',
    labels: {},
    attrs: {},
    observed_at: 0,
    revision: 'r',
  };
}

describe('InteractiveLayout', () => {
  it('springs toward ELK homes then cools down', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');

    layout.seed(
      new Map([['svc:web', { x: 100, y: 50 }]]),
      new Map([['stack:demo', { x: -50, y: -50, width: 400, height: 300 }]]),
      new Map(),
      new Map([['stack:demo', ['svc:web']]]),
      [web],
    );

    expect(layout.active).toBe(true);
    for (let i = 0; i < 200; i += 1) layout.step();
    expect(layout.active).toBe(false);

    const pos = layout.positionsMap().get('svc:web')!;
    expect(pos.x).toBeCloseTo(100, 0);
    expect(pos.y).toBeCloseTo(50, 0);
  });

  it('clamps a dragged node inside its parent group', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 220, y: 0 }],
      ]),
      new Map([['stack:demo', { x: -120, y: -60, width: 480, height: 200 }]]),
      new Map(),
      new Map([['stack:demo', ['svc:web', 'svc:api']]]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    layout.move('svc:web', { x: -2000, y: -2000 });
    const pos = layout.positionsMap().get('svc:web')!;
    const box = layout.groups().get('stack:demo')!;
    expect(pos.x).toBeGreaterThanOrEqual(box.x);
    expect(pos.y).toBeGreaterThanOrEqual(box.y);
    expect(pos.x).toBeLessThanOrEqual(box.x + box.width);
    expect(pos.y).toBeLessThanOrEqual(box.y + box.height);
  });

  it('snaps back home after release', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 240, y: 0 }],
      ]),
      new Map([['stack:demo', { x: -200, y: -120, width: 600, height: 280 }]]),
      new Map(),
      new Map([['stack:demo', ['svc:web', 'svc:api']]]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    layout.move('svc:web', { x: 180, y: 80 });
    layout.release('svc:web');
    for (let i = 0; i < 220; i += 1) layout.step();

    const pos = layout.positionsMap().get('svc:web')!;
    expect(pos.x).toBeCloseTo(0, 0);
    expect(pos.y).toBeCloseTo(0, 0);
    expect(layout.active).toBe(false);
  });

  it('lands home after a flung release', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');

    layout.seed(
      new Map([['svc:web', { x: 0, y: 0 }]]),
      new Map([['stack:demo', { x: -300, y: -300, width: 600, height: 600 }]]),
      new Map(),
      new Map([['stack:demo', ['svc:web']]]),
      [web],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    layout.move('svc:web', { x: 120, y: 90 });
    // A hard throw: the card must overshoot and still settle on its seat.
    layout.release('svc:web', { x: 60, y: 45 });
    for (let i = 0; i < 400; i += 1) layout.step();

    const pos = layout.positionsMap().get('svc:web')!;
    expect(pos.x).toBeCloseTo(0, 0);
    expect(pos.y).toBeCloseTo(0, 0);
    expect(layout.active).toBe(false);
  });

  it('returns home even when a sibling sits on the path', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 0, y: 160 }],
      ]),
      new Map([['stack:demo', { x: -200, y: -80, width: 500, height: 420 }]]),
      new Map(),
      new Map([['stack:demo', ['svc:web', 'svc:api']]]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    // Drag web onto api's seat — the classic jam case.
    layout.move('svc:web', { x: 0, y: 160 });
    layout.release('svc:web');
    for (let i = 0; i < 240; i += 1) layout.step();

    const webPos = layout.positionsMap().get('svc:web')!;
    const apiPos = layout.positionsMap().get('svc:api')!;
    expect(webPos.x).toBeCloseTo(0, 0);
    expect(webPos.y).toBeCloseTo(0, 0);
    expect(apiPos.x).toBeCloseTo(0, 0);
    expect(apiPos.y).toBeCloseTo(160, 0);
  });

  it('keeps pulling home while blocked, then slides in when the path opens', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 400, y: 0 }],
      ]),
      new Map([
        ['stack:front', { x: -100, y: -80, width: 200, height: 160 }],
        ['stack:back', { x: 300, y: -80, width: 200, height: 160 }],
      ]),
      new Map(),
      new Map([
        ['stack:front', ['svc:web']],
        ['stack:back', ['svc:api']],
      ]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    const backHome = { ...layout.groups().get('stack:back')! };

    // Hold front on back's seat (still dragging) — back yields but keeps its home.
    layout.translateGroup('stack:front', 400, 0);
    for (let i = 0; i < 40; i += 1) layout.step();

    const backPressing = layout.groups().get('stack:back')!;
    expect(Math.hypot(backPressing.x - backHome.x, backPressing.y - backHome.y)).toBeGreaterThan(10);

    // Clear the path and drop — back finishes the trip home.
    layout.translateGroup('stack:front', 400, 300);
    layout.releaseGroup('stack:front');
    for (let i = 0; i < 300; i += 1) layout.step();

    const backAfter = layout.groups().get('stack:back')!;
    expect(backAfter.x).toBeCloseTo(backHome.x, 0);
    expect(backAfter.y).toBeCloseTo(backHome.y, 0);
  });

  it('keeps an aspiring stack out while a card inside the blocking stack is held', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 400, y: 0 }],
      ]),
      new Map([
        ['stack:front', { x: -100, y: -80, width: 200, height: 160 }],
        ['stack:back', { x: 300, y: -80, width: 200, height: 160 }],
      ]),
      new Map(),
      new Map([
        ['stack:front', ['svc:web']],
        ['stack:back', ['svc:api']],
      ]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    const backHome = { ...layout.groups().get('stack:back')! };

    // Hold front on back's seat while playing a card inside front.
    layout.translateGroup('stack:front', 400, 0);
    const frontWhileHeld = layout.groups().get('stack:front')!;
    layout.move('svc:web', { x: frontWhileHeld.x + 100, y: frontWhileHeld.y + 40 });
    for (let i = 0; i < 60; i += 1) layout.step();

    const front = layout.groups().get('stack:front')!;
    const back = layout.groups().get('stack:back')!;
    const gap = 24;
    const overlapX =
      (front.width + back.width) / 2 + gap - Math.abs(front.x + front.width / 2 - (back.x + back.width / 2));
    const overlapY =
      (front.height + back.height) / 2 +
      gap -
      Math.abs(front.y + front.height / 2 - (back.y + back.height / 2));
    expect(overlapX <= 0 || overlapY <= 0).toBe(true);
    expect(Math.hypot(back.x - backHome.x, back.y - backHome.y)).toBeGreaterThan(5);

    layout.release('svc:web');
    layout.releaseGroup('stack:front');
  });

  it('slides a stack out of an occupied seat instead of teleporting it on drop', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 400, y: 0 }],
      ]),
      new Map([
        ['stack:front', { x: -100, y: -80, width: 200, height: 160 }],
        ['stack:back', { x: 300, y: -80, width: 200, height: 160 }],
      ]),
      new Map(),
      new Map([
        ['stack:front', ['svc:web']],
        ['stack:back', ['svc:api']],
      ]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    // Drop front squarely on back's seat — the correction is ~180px.
    layout.translateGroup('stack:front', 400, 0);
    const drop = { ...layout.groups().get('stack:front')! };
    layout.releaseGroup('stack:front');

    // Releasing must not move the frame: the drop is where the pointer left it.
    const onRelease = layout.groups().get('stack:front')!;
    expect(onRelease.x).toBeCloseTo(drop.x, 0);
    expect(onRelease.y).toBeCloseTo(drop.y, 0);

    // The whole correction is then played as motion, a spring step at a time.
    let prev = { ...onRelease };
    let biggestStep = 0;
    for (let i = 0; i < 200; i += 1) {
      layout.step();
      const now = layout.groups().get('stack:front')!;
      biggestStep = Math.max(biggestStep, Math.hypot(now.x - prev.x, now.y - prev.y));
      prev = { ...now };
    }
    // Bounded by the spring's own speed clamp — nowhere near the 180px jump.
    expect(biggestStep).toBeLessThanOrEqual(40);

    const settled = layout.groups().get('stack:front')!;
    expect(Math.hypot(settled.x - drop.x, settled.y - drop.y)).toBeGreaterThan(100);
    expect(layout.debugDump().overlaps).toHaveLength(0);
  });

  it('never leaves two stack homes permanently overlapping after a drop', () => {
    const layout = new InteractiveLayout();
    const web = node('svc:web', 'service', 'web');
    const api = node('svc:api', 'service', 'api');

    layout.seed(
      new Map([
        ['svc:web', { x: 0, y: 0 }],
        ['svc:api', { x: 400, y: 0 }],
      ]),
      new Map([
        ['stack:front', { x: -100, y: -80, width: 200, height: 160 }],
        ['stack:back', { x: 300, y: -80, width: 200, height: 160 }],
      ]),
      new Map(),
      new Map([
        ['stack:front', ['svc:web']],
        ['stack:back', ['svc:api']],
      ]),
      [web, api],
    );
    for (let i = 0; i < 200; i += 1) layout.step();

    layout.translateGroup('stack:front', 400, 0);
    layout.releaseGroup('stack:front');
    for (let i = 0; i < 120; i += 1) layout.step();

    const dump = layout.debugDump();
    expect(dump.homeConflicts).toHaveLength(0);
    expect(dump.overlaps).toHaveLength(0);
  });
});
