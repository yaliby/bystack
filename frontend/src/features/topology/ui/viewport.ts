/**
 * Viewport zoom math — shared by wheel, pinch, and ± buttons.
 *
 * Coordinates (`px`, `py`, pinch midpoint) are canvas-centre-relative screen
 * pixels, matching what `TopologyCanvas` feeds `onWheel`.
 */

export interface Viewport {
  readonly x: number;
  readonly y: number;
  readonly zoom: number;
}

export const MIN_ZOOM = 0.08;
export const MAX_ZOOM = 6;
/** Factor applied by the narrow-chrome ± controls. */
export const ZOOM_BUTTON_FACTOR = 1.25;

export function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/** Scale `viewport` by `factor`, keeping the point (`px`, `py`) fixed on screen. */
export function zoomAt(
  viewport: Viewport,
  px: number,
  py: number,
  factor: number,
): Viewport {
  if (!Number.isFinite(factor) || factor <= 0) return viewport;
  const zoom = clampZoom(viewport.zoom * factor);
  if (zoom === viewport.zoom && factor === 1) return viewport;
  const scale = zoom / viewport.zoom;
  return {
    zoom,
    x: px - (px - viewport.x) * scale,
    y: py - (py - viewport.y) * scale,
  };
}

/**
 * Pinch: scale by the ratio of finger distances, anchored at the midpoint
 * between the two contacts (canvas-centre-relative).
 */
export function pinchZoom(
  viewport: Viewport,
  prevDist: number,
  nextDist: number,
  midpoint: { readonly x: number; readonly y: number },
): Viewport {
  if (prevDist <= 0 || nextDist <= 0) return viewport;
  if (!Number.isFinite(prevDist) || !Number.isFinite(nextDist)) return viewport;
  return zoomAt(viewport, midpoint.x, midpoint.y, nextDist / prevDist);
}
