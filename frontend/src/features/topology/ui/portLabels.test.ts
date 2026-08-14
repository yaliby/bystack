import { beforeEach, describe, expect, it } from 'vitest';
import type { Point } from '../layout/elkLayout';
import { clearPortLabelSeats, placePortLabels, type Rect } from './portLabels';

const CHIP_H = 16;

function distanceToPath(at: Point, path: readonly Point[]): number {
  let best = Infinity;
  for (let i = 1; i < path.length; i += 1) {
    const a = path[i - 1];
    const b = path[i];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const lenSq = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((at.x - a.x) * dx + (at.y - a.y) * dy) / lenSq));
    best = Math.min(best, Math.hypot(at.x - (a.x + t * dx), at.y - (a.y + t * dy)));
  }
  return best;
}

function box(at: Point, width: number): Rect {
  return {
    left: at.x - width / 2,
    right: at.x + width / 2,
    top: at.y - CHIP_H / 2,
    bottom: at.y + CHIP_H / 2,
  };
}

function overlaps(a: Rect, b: Rect): boolean {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

describe('placePortLabels', () => {
  beforeEach(() => {
    clearPortLabelSeats();
  });

  it('sets the chip beside its wire, not on it', () => {
    const path = [
      { x: 0, y: 0 },
      { x: 400, y: 0 },
    ];
    const [placed] = placePortLabels([{ key: 'k', path, width: 80 }], [], [], [path], CHIP_H);

    const off = distanceToPath(placed.at, path);
    // Clear of the stroke...
    expect(off).toBeGreaterThan(CHIP_H / 2);
    // ...but unmistakably attached to it.
    expect(off).toBeLessThan(40);
    expect(placed.clear).toBe(true);
  });

  it('never parks a chip on a card or a stack frame', () => {
    // A wire crossing the open lane between a stack frame and the host card.
    const frame: Rect = { left: -400, right: 0, top: -200, bottom: 200 };
    const host: Rect = { left: 200, right: 400, top: -40, bottom: 40 };
    const path = [
      { x: -300, y: 0 },
      { x: 300, y: 0 },
    ];

    const [placed] = placePortLabels([{ key: 'k', path, width: 80 }], [host], [frame], [path], CHIP_H);

    expect(overlaps(box(placed.at, 80), frame)).toBe(false);
    expect(overlaps(box(placed.at, 80), host)).toBe(false);
    expect(placed.clear).toBe(true);
  });

  it('does not anchor to the stretch of wire hidden under the host card', () => {
    // Routes end at the node centre, so the tail of this path is painted over.
    const host: Rect = { left: 200, right: 420, top: -50, bottom: 50 };
    const path = [
      { x: -300, y: 0 },
      { x: 310, y: 0 },
    ];

    const [placed] = placePortLabels([{ key: 'k', path, width: 80 }], [host], [], [path], CHIP_H);

    // The chip has to sit where its own wire can still be seen.
    expect(placed.at.x).toBeLessThan(host.left);
  });

  it('spreads chips that converge on one host instead of stacking them', () => {
    const paths = [
      [
        { x: 0, y: -100 },
        { x: 400, y: -100 },
        { x: 400, y: 0 },
      ],
      [
        { x: 0, y: 0 },
        { x: 400, y: 0 },
      ],
      [
        { x: 0, y: 100 },
        { x: 400, y: 100 },
        { x: 400, y: 0 },
      ],
    ];
    const placed = placePortLabels(
      paths.map((path, i) => ({ key: `k${i}`, path, width: 80 })),
      [],
      [],
      paths,
      CHIP_H,
    );

    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        expect(overlaps(box(placed[i].at, 80), box(placed[j].at, 80))).toBe(false);
      }
      // Each one still belongs to the wire it describes.
      expect(distanceToPath(placed[i].at, paths[i])).toBeLessThan(40);
    }
  });

  it('rides its wire when the wire moves', () => {
    let previous: Point | null = null;
    let worstExcess = 0;

    for (let dy = 0; dy <= 120; dy += 1) {
      const path = [
        { x: -300, y: dy },
        { x: 300, y: dy },
      ];
      const [placed] = placePortLabels([{ key: 'k', path, width: 80 }], [], [], [path], CHIP_H);
      if (previous) {
        const moved = Math.hypot(placed.at.x - previous.x, placed.at.y - previous.y);
        // The wire moved 1px this frame; anything past that is the chip jumping.
        worstExcess = Math.max(worstExcess, moved - 1);
      }
      previous = placed.at;
    }

    expect(worstExcess).toBeLessThanOrEqual(0.01);
  });

  it('keeps its seat while a card drifts past instead of flipping sides', () => {
    // The placement is re-scored every frame, so a card sliding alongside the
    // wire can flip the winner from one side of the stroke to the other and
    // back — the label jumping its own height while nothing else moved 1px.
    let previous: Point | null = null;
    let worstExcess = 0;
    const path = [
      { x: -300, y: 0 },
      { x: 300, y: 0 },
    ];

    for (let t = 0; t <= 120; t += 1) {
      const drifting: Rect = {
        left: -300 + t * 4,
        right: -180 + t * 4,
        top: -70,
        bottom: -10,
      };
      const [placed] = placePortLabels(
        [{ key: 'k', path, width: 80 }],
        [drifting],
        [],
        [path],
        CHIP_H,
      );
      if (previous) {
        worstExcess = Math.max(
          worstExcess,
          Math.hypot(placed.at.x - previous.x, placed.at.y - previous.y),
        );
      }
      previous = placed.at;
    }

    expect(worstExcess).toBeLessThanOrEqual(2);
  });

  it('holds its seat when the route is re-cut under it', () => {
    // Corridors change shape mid-drag, which renumbers every arc length on the
    // path. Matching the seat too strictly loses it on exactly that frame — and
    // losing the match loses the discount holding the label still, so it flips
    // sides just as the wire redraws. Measured against real drags this was a
    // 34px lurch with the wire itself barely moving.
    let previous: Point | null = null;
    let worstExcess = 0;

    for (let t = 0; t <= 60; t += 1) {
      // The elbow migrates along the wire; the labelled run does not move.
      const path = [
        { x: -300, y: 40 },
        { x: -140 + t, y: 40 },
        { x: -140 + t, y: 0 },
        { x: 300, y: 0 },
      ];
      const [placed] = placePortLabels([{ key: 'k', path, width: 80 }], [], [], [path], CHIP_H);
      if (previous) {
        worstExcess = Math.max(
          worstExcess,
          Math.hypot(placed.at.x - previous.x, placed.at.y - previous.y),
        );
      }
      previous = placed.at;
    }

    expect(worstExcess).toBeLessThanOrEqual(12);
  });

  it('prefers a frame to a card when it is boxed in', () => {
    // Drag the host in among the services and nothing near the wire is clear.
    // A chip on the tint is still readable; a chip on a card buries its text.
    const path = [
      { x: 0, y: 0 },
      { x: 160, y: 0 },
    ];
    // Card hard against one side of the wire; bare frame tint on the other.
    const card: Rect = { left: -40, right: 200, top: -70, bottom: -2 };
    const frame: Rect = { left: -400, right: 400, top: -300, bottom: 300 };

    const [placed] = placePortLabels(
      [{ key: 'k', path, width: 80 }],
      [card],
      [frame],
      [path],
      CHIP_H,
    );

    const b = box(placed.at, 80);
    expect(overlaps(b, card)).toBe(false);
    expect(distanceToPath(placed.at, path)).toBeLessThan(70);
  });

  it('always returns a placement, even with nowhere clear to put one', () => {
    const path = [
      { x: 0, y: 0 },
      { x: 200, y: 0 },
    ];
    const wall: Rect = { left: -1000, right: 1000, top: -1000, bottom: 1000 };

    const [placed] = placePortLabels([{ key: 'k', path, width: 80 }], [wall], [], [path], CHIP_H);

    expect(placed.clear).toBe(false);
    // Boxed in is not a licence to drift: it still reads as this wire's label.
    expect(distanceToPath(placed.at, path)).toBeLessThan(40);
  });

  it('holds its seat while the wire is moving, then walks home', () => {
    // A hold that never lets go parks the number wherever a drag left it. A walk
    // that starts while the hand is still on a card is flicker. The bargain is
    // the same seat for as long as the picture is changing, and a creep back
    // toward the unconstrained best once it has stopped.
    const atDy = (dy: number) => {
      const path = [
        { x: -400, y: dy },
        { x: 400, y: dy },
      ];
      return { path, req: [{ key: 'k' as const, path, width: 80 }] };
    };

    const fresh = atDy(0);
    const [ideal] = placePortLabels(fresh.req, [], [], [fresh.path], CHIP_H);

    // Cover the host end so the chip has to sit further down the wire.
    const wall: Rect = { left: 200, right: 500, top: -80, bottom: 80 };
    let at = ideal.at;
    for (let i = 0; i < 20; i += 1) {
      [{ at }] = placePortLabels(fresh.req, [wall], [], [fresh.path], CHIP_H);
    }
    const parked = at.x;
    expect(parked).toBeLessThan(ideal.at.x - 80);

    // Drag: the wall is gone, so the ideal is back near the host, but the wire
    // is still translating. The chip must ride, not walk.
    let worstAlong = 0;
    for (let dy = 1; dy <= 40; dy += 1) {
      const { path, req } = atDy(dy);
      const [placed] = placePortLabels(req, [], [], [path], CHIP_H);
      worstAlong = Math.max(worstAlong, Math.abs(placed.at.x - parked));
      at = placed.at;
    }
    expect(worstAlong).toBeLessThan(20);
    expect(Math.abs(at.x - parked)).toBeLessThan(20);

    // Hand off. After a short settle the chip walks back to the host end.
    const rest = atDy(40);
    for (let i = 0; i < 80; i += 1) {
      [{ at }] = placePortLabels(rest.req, [], [], [rest.path], CHIP_H);
    }
    expect(Math.abs(at.x - ideal.at.x)).toBeLessThan(3);
    expect(Math.abs(at.y - (ideal.at.y + 40))).toBeLessThan(3);
  });
});
