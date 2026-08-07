/**
 * Polyline helpers for ELK edge paths — used by flow-dot animation
 * (same idea as DockGraph's canvas edge dots).
 */

import type { Point } from './elkLayout';

export function polylineLength(points: readonly Point[]): number {
  let len = 0;
  for (let i = 1; i < points.length; i += 1) {
    const dx = points[i].x - points[i - 1].x;
    const dy = points[i].y - points[i - 1].y;
    len += Math.hypot(dx, dy);
  }
  return len;
}

/** Point at fraction t ∈ [0,1] along the polyline. */
export function polylinePointAt(
  points: readonly Point[],
  t: number,
  totalLen: number,
): Point {
  if (points.length < 2 || t <= 0) return points[0];
  if (t >= 1) return points[points.length - 1];

  const target = t * totalLen;
  let accumulated = 0;

  for (let i = 1; i < points.length; i += 1) {
    const dx = points[i].x - points[i - 1].x;
    const dy = points[i].y - points[i - 1].y;
    const seg = Math.hypot(dx, dy);
    if (accumulated + seg >= target) {
      const frac = seg > 0 ? (target - accumulated) / seg : 0;
      return {
        x: points[i - 1].x + dx * frac,
        y: points[i - 1].y + dy * frac,
      };
    }
    accumulated += seg;
  }

  return points[points.length - 1];
}
