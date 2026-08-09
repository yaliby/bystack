import { describe, expect, it } from 'vitest';
import {
  MAX_ZOOM,
  MIN_ZOOM,
  ZOOM_BUTTON_FACTOR,
  clampZoom,
  pinchZoom,
  zoomAt,
  type Viewport,
} from './viewport';

const origin: Viewport = { x: 0, y: 0, zoom: 1 };

describe('clampZoom', () => {
  it('passes through values inside the range', () => {
    expect(clampZoom(1)).toBe(1);
    expect(clampZoom(MIN_ZOOM)).toBe(MIN_ZOOM);
    expect(clampZoom(MAX_ZOOM)).toBe(MAX_ZOOM);
  });

  it('clamps below and above', () => {
    expect(clampZoom(MIN_ZOOM / 2)).toBe(MIN_ZOOM);
    expect(clampZoom(MAX_ZOOM * 2)).toBe(MAX_ZOOM);
  });
});

describe('zoomAt', () => {
  it('scales zoom by factor', () => {
    const next = zoomAt(origin, 0, 0, 2);
    expect(next.zoom).toBe(2);
    expect(next.x).toBe(0);
    expect(next.y).toBe(0);
  });

  it('keeps the anchor point fixed on screen', () => {
    // World point under (100, 50) before zoom: ((100 - 0) / 1, (50 - 0) / 1).
    const before = { x: 40, y: -20, zoom: 1 };
    const next = zoomAt(before, 100, 50, 2);
    expect(next.zoom).toBe(2);
    // Same screen mapping: px = x + world * zoom  ⇒  100 = next.x + 60 * 2
    expect(next.x).toBeCloseTo(100 - (100 - before.x) * 2);
    expect(next.y).toBeCloseTo(50 - (50 - before.y) * 2);
  });

  it('clamps to MAX_ZOOM', () => {
    const next = zoomAt({ x: 0, y: 0, zoom: MAX_ZOOM }, 10, 10, 2);
    expect(next.zoom).toBe(MAX_ZOOM);
  });

  it('clamps to MIN_ZOOM', () => {
    const next = zoomAt({ x: 0, y: 0, zoom: MIN_ZOOM }, 10, 10, 0.5);
    expect(next.zoom).toBe(MIN_ZOOM);
  });

  it('ignores non-positive factors', () => {
    expect(zoomAt(origin, 0, 0, 0)).toEqual(origin);
    expect(zoomAt(origin, 0, 0, -1)).toEqual(origin);
  });

  it('matches the button step factor', () => {
    const in_ = zoomAt(origin, 0, 0, ZOOM_BUTTON_FACTOR);
    expect(in_.zoom).toBeCloseTo(ZOOM_BUTTON_FACTOR);
    const out = zoomAt(in_, 0, 0, 1 / ZOOM_BUTTON_FACTOR);
    expect(out.zoom).toBeCloseTo(1);
  });
});

describe('pinchZoom', () => {
  it('zooms in when fingers move apart', () => {
    const next = pinchZoom(origin, 100, 200, { x: 0, y: 0 });
    expect(next.zoom).toBe(2);
  });

  it('zooms out when fingers move together', () => {
    const next = pinchZoom({ x: 0, y: 0, zoom: 2 }, 200, 100, { x: 0, y: 0 });
    expect(next.zoom).toBe(1);
  });

  it('anchors at the finger midpoint', () => {
    const before = { x: 0, y: 0, zoom: 1 };
    const next = pinchZoom(before, 100, 200, { x: 80, y: -40 });
    expect(next.zoom).toBe(2);
    expect(next.x).toBeCloseTo(80 - (80 - before.x) * 2);
    expect(next.y).toBeCloseTo(-40 - (-40 - before.y) * 2);
  });

  it('is a no-op when either distance is zero or non-finite', () => {
    expect(pinchZoom(origin, 0, 100, { x: 0, y: 0 })).toEqual(origin);
    expect(pinchZoom(origin, 100, 0, { x: 0, y: 0 })).toEqual(origin);
    expect(pinchZoom(origin, Number.NaN, 100, { x: 0, y: 0 })).toEqual(origin);
  });
});
