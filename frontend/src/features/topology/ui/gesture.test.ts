import { describe, expect, it } from 'vitest';
import {
  initialGesture,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  pointerDistance,
  pointerMidpoint,
} from './gesture';

describe('pointer helpers', () => {
  it('measures distance and midpoint', () => {
    expect(pointerDistance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
    expect(pointerMidpoint({ x: 0, y: 0 }, { x: 10, y: 6 })).toEqual({ x: 5, y: 3 });
  });
});

describe('gesture state machine', () => {
  it('idle → single on first finger', () => {
    let g = initialGesture();
    const down = onPointerDown(g, 1, 10, 20);
    g = down.state;
    expect(down.action).toEqual({ type: 'enter-single', pointerId: 1 });
    expect(g.mode).toBe('single');
    expect(g.singleId).toBe(1);
    expect(g.pointers.size).toBe(1);
  });

  it('single → pinch cancels the drag when a second finger lands', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 0, 0).state;
    const second = onPointerDown(g, 2, 100, 0);
    g = second.state;
    expect(second.action).toEqual({ type: 'enter-pinch', cancelSingle: true });
    expect(g.mode).toBe('pinch');
    expect(g.singleId).toBeNull();
    expect(g.pinchDist).toBe(100);
  });

  it('emits pinch deltas as fingers move', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 0, 0).state;
    g = onPointerDown(g, 2, 100, 0).state;

    const moved = onPointerMove(g, 2, 200, 0);
    g = moved.state;
    expect(moved.action.type).toBe('pinch');
    if (moved.action.type !== 'pinch') throw new Error('expected pinch');
    expect(moved.action.prevDist).toBe(100);
    expect(moved.action.nextDist).toBe(200);
    expect(moved.action.mid).toEqual({ x: 100, y: 0 });
    expect(g.pinchDist).toBe(200);
  });

  it('does not emit pinch while in single mode', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 0, 0).state;
    const moved = onPointerMove(g, 1, 30, 40);
    expect(moved.action.type).toBe('noop');
    expect(moved.state.mode).toBe('single');
  });

  it('pinch → idle when a finger lifts (even if one remains)', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 0, 0).state;
    g = onPointerDown(g, 2, 100, 0).state;
    const up = onPointerUp(g, 2);
    g = up.state;
    expect(up.action.type).toBe('leave-pinch');
    expect(g.mode).toBe('idle');
    expect(g.pointers.size).toBe(1);
    expect(g.pinchDist).toBe(0);
  });

  it('single → idle on pointer up', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 5, 5).state;
    const up = onPointerUp(g, 1);
    expect(up.action.type).toBe('leave-single');
    expect(up.state.mode).toBe('idle');
    expect(up.state.pointers.size).toBe(0);
  });

  it('ignores a duplicate down for the same pointer id', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 0, 0).state;
    const again = onPointerDown(g, 1, 50, 50);
    expect(again.action.type).toBe('noop');
    expect(again.state.pointers.get(1)).toEqual({ x: 0, y: 0 });
  });

  it('ignores up for an unknown pointer', () => {
    const g = initialGesture();
    const up = onPointerUp(g, 99);
    expect(up.action.type).toBe('noop');
    expect(up.state).toBe(g);
  });

  it('keeps pinch alive when a third finger lifts and two remain', () => {
    let g = initialGesture();
    g = onPointerDown(g, 1, 0, 0).state;
    g = onPointerDown(g, 2, 100, 0).state;
    g = onPointerDown(g, 3, 50, 50).state;
    expect(g.mode).toBe('pinch');
    expect(g.pointers.size).toBe(3);

    const up = onPointerUp(g, 3);
    expect(up.action.type).toBe('noop');
    expect(up.state.mode).toBe('pinch');
    expect(up.state.pointers.size).toBe(2);
  });
});
