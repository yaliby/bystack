/**
 * Multi-touch gesture mode for the topology canvas.
 *
 * Pure state machine: the canvas owns drag/pan physics; this module only
 * decides when a press is a single-finger gesture vs a two-finger pinch, and
 * emits pinch distance updates. A second finger always cancels an in-flight
 * single-finger drag so node physics never run under a pinch.
 */

export interface Point2 {
  readonly x: number;
  readonly y: number;
}

export type GestureMode = 'idle' | 'single' | 'pinch';

export interface GestureState {
  readonly mode: GestureMode;
  readonly pointers: ReadonlyMap<number, Point2>;
  /** Distance between the two active pinch fingers; 0 outside pinch mode. */
  readonly pinchDist: number;
  /** The pointer that owns the single-finger gesture, if any. */
  readonly singleId: number | null;
}

export type GestureAction =
  | { readonly type: 'noop' }
  | { readonly type: 'enter-single'; readonly pointerId: number }
  | { readonly type: 'enter-pinch'; readonly cancelSingle: boolean }
  | {
      readonly type: 'pinch';
      readonly prevDist: number;
      readonly nextDist: number;
      readonly mid: Point2;
    }
  | { readonly type: 'leave-pinch' }
  | { readonly type: 'leave-single' };

export function initialGesture(): GestureState {
  return {
    mode: 'idle',
    pointers: new Map(),
    pinchDist: 0,
    singleId: null,
  };
}

export function pointerDistance(a: Point2, b: Point2): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function pointerMidpoint(a: Point2, b: Point2): Point2 {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function pair(pointers: ReadonlyMap<number, Point2>): [Point2, Point2] | null {
  if (pointers.size < 2) return null;
  const pts = [...pointers.values()];
  return [pts[0], pts[1]];
}

function withPointers(
  state: GestureState,
  pointers: Map<number, Point2>,
  patch: Partial<GestureState> = {},
): GestureState {
  return { ...state, pointers, ...patch };
}

export function onPointerDown(
  state: GestureState,
  id: number,
  x: number,
  y: number,
): { state: GestureState; action: GestureAction } {
  if (state.pointers.has(id)) {
    return { state, action: { type: 'noop' } };
  }
  const pointers = new Map(state.pointers);
  pointers.set(id, { x, y });

  if (pointers.size === 1) {
    return {
      state: withPointers(state, pointers, {
        mode: 'single',
        singleId: id,
        pinchDist: 0,
      }),
      action: { type: 'enter-single', pointerId: id },
    };
  }

  if (pointers.size === 2) {
    const ends = pair(pointers);
    const dist = ends ? pointerDistance(ends[0], ends[1]) : 0;
    const cancelSingle = state.mode === 'single';
    return {
      state: withPointers(state, pointers, {
        mode: 'pinch',
        singleId: null,
        pinchDist: dist,
      }),
      action: { type: 'enter-pinch', cancelSingle },
    };
  }

  // Third+ finger: track position but stay in pinch with the first pair.
  return {
    state: withPointers(state, pointers),
    action: { type: 'noop' },
  };
}

export function onPointerMove(
  state: GestureState,
  id: number,
  x: number,
  y: number,
): { state: GestureState; action: GestureAction } {
  if (!state.pointers.has(id)) {
    return { state, action: { type: 'noop' } };
  }
  const pointers = new Map(state.pointers);
  pointers.set(id, { x, y });

  if (state.mode !== 'pinch') {
    return {
      state: withPointers(state, pointers),
      action: { type: 'noop' },
    };
  }

  const ends = pair(pointers);
  if (!ends) {
    return {
      state: withPointers(state, pointers),
      action: { type: 'noop' },
    };
  }

  const nextDist = pointerDistance(ends[0], ends[1]);
  const prevDist = state.pinchDist;
  const mid = pointerMidpoint(ends[0], ends[1]);
  return {
    state: withPointers(state, pointers, { pinchDist: nextDist }),
    action: {
      type: 'pinch',
      prevDist,
      nextDist,
      mid,
    },
  };
}

export function onPointerUp(
  state: GestureState,
  id: number,
): { state: GestureState; action: GestureAction } {
  if (!state.pointers.has(id)) {
    return { state, action: { type: 'noop' } };
  }
  const pointers = new Map(state.pointers);
  pointers.delete(id);

  if (state.mode === 'pinch') {
    if (pointers.size >= 2) {
      const ends = pair(pointers);
      const dist = ends ? pointerDistance(ends[0], ends[1]) : 0;
      return {
        state: withPointers(state, pointers, { pinchDist: dist }),
        action: { type: 'noop' },
      };
    }
    // Fewer than two fingers: leave pinch. Remaining contact (if any) does
    // not resume a single-finger drag — that would steal a click mid-gesture.
    return {
      state: withPointers(state, pointers, {
        mode: 'idle',
        singleId: null,
        pinchDist: 0,
      }),
      action: { type: 'leave-pinch' },
    };
  }

  if (state.mode === 'single' && state.singleId === id) {
    return {
      state: withPointers(state, pointers, {
        mode: 'idle',
        singleId: null,
        pinchDist: 0,
      }),
      action: { type: 'leave-single' },
    };
  }

  return {
    state: withPointers(state, pointers, {
      mode: pointers.size === 0 ? 'idle' : state.mode,
      singleId: pointers.size === 0 ? null : state.singleId,
    }),
    action: { type: 'noop' },
  };
}
