import { describe, expect, it } from 'vitest';
import { polylineLength, polylinePointAt } from './polyline';

describe('polyline', () => {
  it('measures total length', () => {
    expect(polylineLength([{ x: 0, y: 0 }, { x: 30, y: 40 }])).toBe(50);
  });

  it('samples midpoints along a path', () => {
    const pts = [
      { x: 0, y: 0 },
      { x: 100, y: 0 },
      { x: 100, y: 100 },
    ];
    const len = polylineLength(pts);
    expect(len).toBe(200);
    const mid = polylinePointAt(pts, 0.5, len);
    expect(mid.x).toBeCloseTo(100);
    expect(mid.y).toBeCloseTo(0);
    const threeQuarter = polylinePointAt(pts, 0.75, len);
    expect(threeQuarter.x).toBeCloseTo(100);
    expect(threeQuarter.y).toBeCloseTo(50);
  });
});
