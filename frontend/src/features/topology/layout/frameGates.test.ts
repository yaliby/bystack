import { beforeEach, describe, expect, it } from 'vitest';
import { assignFrameGates, clearFrameGates, type GateCrossing, type GateFrame } from './frameGates';

const frame: GateFrame = { id: 'frame:stack', x: 0, y: 0, hw: 200, hh: 100 };

/** A link headed for (x, y), starting from `from` inside the frame. */
function crossing(
  id: string,
  x: number,
  y: number,
  from: { x: number; y: number } = { x: 0, y: 0 },
): GateCrossing {
  return { id, frame: frame.id, target: { x, y }, from };
}

describe('assignFrameGates', () => {
  beforeEach(() => {
    clearFrameGates();
  });

  it('puts a lone link opposite its own card on the face its target is on', () => {
    const gates = assignFrameGates([frame], [crossing('a', 900, 0, { x: -100, y: 40 })]);
    const gate = gates.get('a')!;
    expect(gate.face).toBe('right');
    expect(gate.point).toEqual({ x: 200, y: 40 });
    expect(gate.outward).toEqual({ x: 1, y: 0 });
  });

  it('never leaves by the face pointing away from the target', () => {
    const gates = assignFrameGates([frame], [crossing('a', 0, -900)]);
    expect(gates.get('a')!.face).toBe('top');

    clearFrameGates();
    const below = assignFrameGates([frame], [crossing('b', 0, 900)]);
    expect(below.get('b')!.face).toBe('bottom');
  });

  it('sends each link out by the face nearest its own target', () => {
    const gates = assignFrameGates(
      [frame],
      [crossing('right', 900, 0), crossing('up', 0, -900), crossing('down', 0, 900)],
    );

    expect([...gates.values()].map((g) => g.face)).toEqual(['right', 'top', 'bottom']);
  });

  it('keeps links headed the same way on the same face', () => {
    const gates = assignFrameGates(
      [frame],
      [crossing('a', 900, -20), crossing('b', 900, 0), crossing('c', 900, 20)],
    );
    expect([...gates.values()].every((g) => g.face === 'right')).toBe(true);
  });

  it('takes the face pointing the way the wire is going when two are equidistant', () => {
    // Beyond the top-right corner every face that meets there is the same
    // distance away, so the tie has to be broken by where the wire is headed:
    // mostly rightwards here, so out through the right.
    const gates = assignFrameGates([frame], [crossing('a', 900, -200)]);
    expect(gates.get('a')!.face).toBe('right');

    clearFrameGates();
    const upward = assignFrameGates([frame], [crossing('b', 400, -800)]);
    expect(upward.get('b')!.face).toBe('top');
  });

  it('gives each link a gate beside its own card, in the cards’ own order', () => {
    // Both links go to the same host, so nothing about the target can order
    // them: the near card must get the near gate or the two cross inside.
    const gates = assignFrameGates(
      [frame],
      [
        crossing('low', 900, 0, { x: -120, y: 60 }),
        crossing('high', 900, 0, { x: -120, y: -60 }),
      ],
    );

    expect(gates.get('high')!.point.y).toBe(-60);
    expect(gates.get('low')!.point.y).toBe(60);
  });

  it('pushes gates apart when their cards ask for the same stretch of face', () => {
    const gates = assignFrameGates(
      [frame],
      [
        crossing('a', 900, 0, { x: -120, y: 0 }),
        crossing('b', 900, 0, { x: -120, y: 4 }),
      ],
    );

    const ys = [...gates.values()].map((g) => g.point.y).sort((a, b) => a - b);
    expect(ys[1] - ys[0]).toBeGreaterThanOrEqual(30);
    // Centred on what the pair asked for, rather than both shunted one way.
    expect((ys[0] + ys[1]) / 2).toBeCloseTo(2, 0);
  });

  it('keeps gates on the face, clear of its corners', () => {
    const gates = assignFrameGates(
      [frame],
      [
        crossing('above', 900, 0, { x: -120, y: -400 }),
        crossing('below', 900, 0, { x: -120, y: 400 }),
      ],
    );

    for (const gate of gates.values()) {
      expect(Math.abs(gate.point.y)).toBeLessThanOrEqual(70);
    }
  });

  it('falls back to even divisions once a face cannot hold them all apart', () => {
    const gates = assignFrameGates(
      [frame],
      [0, 1, 2, 3, 4, 5].map((i) => crossing(`link${i}`, 900, 0, { x: -120, y: 0 })),
    );

    // A 200-tall face carrying six gates: sevenths, symmetric about its centre.
    const ys = [...gates.values()].map((g) => Math.round(g.point.y)).sort((a, b) => a - b);
    expect(ys).toEqual([-71, -43, -14, 14, 43, 71]);
  });

  it('holds a face while its target only drifts', () => {
    const first = assignFrameGates([frame], [crossing('a', 600, 560)]);
    const face = first.get('a')!.face;
    // Straight out along the diagonal, where two faces are equidistant by
    // construction: the wire must not trade sides on every frame.
    for (let step = 0; step < 6; step += 1) {
      const gates = assignFrameGates([frame], [crossing('a', 600 + step, 560 - step)]);
      expect(gates.get('a')!.face).toBe(face);
    }
  });

  it('forgets a frame that no longer has links crossing it', () => {
    assignFrameGates([frame], [crossing('a', 900, 0)]);
    const empty = assignFrameGates([frame], []);
    expect(empty.size).toBe(0);
    // The face is re-decided from scratch, not from the frame that left.
    const again = assignFrameGates([frame], [crossing('a', -900, 0)]);
    expect(again.get('a')!.face).toBe('left');
  });
});
