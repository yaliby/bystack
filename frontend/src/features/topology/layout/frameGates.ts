/**
 * Where a link leaves the block it lives in.
 *
 * A wire crossing a stack frame used to break out wherever its corridor
 * happened to meet the border, which is why three links to the same host left
 * one assembly at three unrelated points — one through a corner, one grazing
 * the title, one halfway down a side. A frame is a thing with sides, and cables
 * leave a rack through sockets, not through the sheet metal.
 *
 * So every crossing is given a *gate*: a point on the face nearest to where
 * that link is headed, opposite the card the wire comes from. A socket belongs
 * where its cable already is. Cutting the face into even divisions instead —
 * one gate at its middle, two at its thirds — spread the gates prettily and
 * routed appallingly: two links to the same host got their thirds in whichever
 * order their keys sorted, so the card nearest the exit was regularly sent to
 * the far gate and had to cross the whole assembly, and its neighbour crossed
 * back the other way. A gate opposite its own card cannot do that.
 *
 * Gates on one face are then pushed apart to `GATE_GAP` in the order their
 * cards sit along it, so wires that leave together stay parallel and never
 * trade places. Where a face is too crowded to hold them all at that spacing,
 * it falls back to even divisions — the only case the old rule was right for.
 *
 * Spreading the wires over more faces instead was tried and looked worse: a
 * second link to a host on the right was pushed out through the top and had to
 * travel the length of the block to get back, which reads as an obstruction.
 * Wires that are going the same way should leave the same way.
 *
 * Only three faces are ever in play. The fourth points away from the target,
 * and a wire that left by it would have to come back around the block it just
 * left. The other two matter for holding a gate still: a face stays chosen
 * while it is still one of the three facing the target.
 */

import type { Point } from './elkLayout';

export type Face = 'top' | 'right' | 'bottom' | 'left';

/** Fixed order, so equidistant faces break their tie the same way every frame. */
const FACES: readonly Face[] = ['top', 'right', 'bottom', 'left'];

/** Every face but the one pointing away from the target. */
const FACES_IN_PLAY = 3;

/**
 * How much closer a rival face must be before a wire moves its gate to it.
 *
 * Moving a gate is a jump of at least half a frame, so a target drifting along
 * a diagonal — where two faces are equidistant by construction — must not be
 * able to trade the wire back and forth. It keeps the face it has while that
 * face is still one of the three in play and still nearly the closest.
 */
const FACE_HOLD_PX = 60;

/**
 * How far a card must overtake its neighbour before two gates swap slots.
 *
 * Gates on one face are ordered by where their own cards sit along it, so a
 * swap moves both wires by a whole slot. Re-deciding that on raw position lets
 * two cards level with each other trade places on noise for as long as they
 * stay level — see the same threshold on fan slots in `liveEdges`.
 */
const SLOT_HYSTERESIS = 26;

/**
 * How far apart two gates on one face sit.
 *
 * Wide enough that the wires leaving them read as two, which is the whole
 * point of giving them separate sockets; `liveEdges` spaces the lanes they run
 * out into by `PORT`, so anything much tighter here would be undone outside.
 */
const GATE_GAP = 30;

/**
 * How close to a corner a gate may sit.
 *
 * A gate in a corner has no square way out — the run-up would graze the
 * adjacent face — and on the top face the near corner is where the frame
 * writes its name.
 */
const FACE_MARGIN = 30;

/** Axis-aligned block a link crosses on its way out (centre + half extents). */
export interface GateFrame {
  readonly id: string;
  readonly x: number;
  readonly y: number;
  readonly hw: number;
  readonly hh: number;
}

/** One link's crossing of one frame. */
export interface GateCrossing {
  /** Unique per (link, frame) — this is what carries a gate across frames. */
  readonly id: string;
  readonly frame: string;
  /** Where the link is headed, on the far side of the border. */
  readonly target: Point;
  /** The card inside the frame this link runs to — the gate sits opposite it. */
  readonly from: Point;
}

export interface Gate {
  readonly face: Face;
  /** On the frame's border. */
  readonly point: Point;
  /** Unit normal, pointing out of the frame. */
  readonly outward: Point;
}

interface FrameMemory {
  /** Face each crossing left by last frame. */
  face: Map<string, Face>;
  /** Slot order per face, as crossing ids. */
  order: Map<Face, string[]>;
}

const memory = new Map<string, FrameMemory>();

/** Drop remembered gates (tests / full graph replace). */
export function clearFrameGates(): void {
  memory.clear();
}

/**
 * Place a gate for every crossing, one frame at a time.
 *
 * Crossings naming a frame that is not in `frames` are dropped: a link cannot
 * leave a block that is no longer on the canvas.
 */
export function assignFrameGates(
  frames: readonly GateFrame[],
  crossings: readonly GateCrossing[],
): Map<string, Gate> {
  const byFrame = new Map<string, GateCrossing[]>();
  for (const crossing of crossings) {
    const list = byFrame.get(crossing.frame) ?? [];
    list.push(crossing);
    byFrame.set(crossing.frame, list);
  }

  const out = new Map<string, Gate>();
  for (const frame of frames) {
    const links = byFrame.get(frame.id);
    if (!links || links.length === 0) continue;
    for (const [id, gate] of gatesForFrame(frame, links)) out.set(id, gate);
  }

  for (const id of [...memory.keys()]) {
    if (!byFrame.has(id)) memory.delete(id);
  }
  return out;
}

interface Rect {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
}

interface Ranked {
  readonly face: Face;
  readonly dist: number;
}

function gatesForFrame(frame: GateFrame, links: readonly GateCrossing[]): Map<string, Gate> {
  const rect = rectOf(frame);
  const mem = memory.get(frame.id) ?? { face: new Map(), order: new Map() };

  // The three faces each link may leave by, nearest first.
  const ranked = new Map<string, Ranked[]>();
  for (const link of links) {
    const scored = FACES.map((face) => ({ face, dist: faceDistance(rect, face, link.target) }))
      .sort(
        (a, b) =>
          a.dist - b.dist ||
          reach(rect, b.face, link.target) - reach(rect, a.face, link.target) ||
          FACES.indexOf(a.face) - FACES.indexOf(b.face),
      )
      .slice(0, FACES_IN_PLAY);
    ranked.set(link.id, scored);
  }

  const placed = new Map<string, Face>();
  for (const link of links) {
    placed.set(link.id, chooseFace(ranked.get(link.id)!, mem.face.get(link.id)));
  }

  const onFace = new Map<Face, string[]>();
  for (const link of links) {
    const face = placed.get(link.id)!;
    onFace.set(face, [...(onFace.get(face) ?? []), link.id]);
  }

  const cards = new Map(links.map((link) => [link.id, link.from]));
  const nextOrder = new Map<Face, string[]>();
  const out = new Map<string, Gate>();

  for (const [face, ids] of onFace) {
    // Wires cross each other inside the block for nothing if the gate order
    // along a face disagrees with the order of the cards behind them.
    const sorted = stableAlong(face, ids, cards, mem.order.get(face) ?? []);
    nextOrder.set(face, sorted);
    const seats = seatsAlong(
      rect,
      face,
      sorted.map((id) => alongAxis(face, cards.get(id)!)),
    );
    sorted.forEach((id, index) => {
      out.set(id, gateAt(rect, face, seats[index]));
    });
  }

  memory.set(frame.id, { face: placed, order: nextOrder });
  return out;
}

/**
 * Where the gates on one face actually sit: each opposite its own card, moved
 * only as far as it must be to keep `GATE_GAP` from its neighbours.
 *
 * `wanted` is in face-axis coordinates, already in gate order. Gates that ask
 * for the same stretch of face are gathered into a run and the run is centred
 * on what they asked for between them, so two cards level with each other end
 * up either side of where a single one would have gone rather than both being
 * shunted the same way.
 */
function seatsAlong(rect: Rect, face: Face, wanted: readonly number[]): number[] {
  const span = face === 'left' || face === 'right' ? rect.bottom - rect.top : rect.right - rect.left;
  const origin = face === 'left' || face === 'right' ? rect.top : rect.left;
  const count = wanted.length;

  if (count === 0) return [];
  // Too many gates for the face to hold them apart: fall back to even
  // divisions, which at least stay on the face and stay symmetric.
  if ((count - 1) * GATE_GAP > span - FACE_MARGIN * 2) {
    return wanted.map((_, index) => origin + (span * (index + 1)) / (count + 1));
  }

  const lo = origin + FACE_MARGIN;
  const hi = origin + span - FACE_MARGIN;

  // Runs of gates that have to move as one block, each holding the sum of what
  // its members asked for so the block can be centred on their average.
  const runs: { size: number; sum: number }[] = [];
  const firstOf = (run: { size: number; sum: number }) =>
    run.sum / run.size - ((run.size - 1) * GATE_GAP) / 2;

  for (const at of wanted) {
    runs.push({ size: 1, sum: Math.min(hi, Math.max(lo, at)) });
    while (runs.length > 1) {
      const next = runs[runs.length - 1];
      const prev = runs[runs.length - 2];
      if (firstOf(next) >= firstOf(prev) + prev.size * GATE_GAP) break;
      runs.splice(runs.length - 2, 2, {
        size: prev.size + next.size,
        sum: prev.sum + next.sum,
      });
    }
  }

  const seats: number[] = [];
  for (const run of runs) {
    const first = Math.min(Math.max(firstOf(run), lo), hi - (run.size - 1) * GATE_GAP);
    for (let i = 0; i < run.size; i += 1) seats.push(first + i * GATE_GAP);
  }

  // Clamping a run to the face can push it back into its neighbour; there is
  // only one direction left to give, so a monotone pass settles it.
  for (let i = 1; i < count; i += 1) {
    seats[i] = Math.max(seats[i], seats[i - 1] + GATE_GAP);
  }
  for (let i = count - 1; i >= 0; i -= 1) {
    seats[i] = Math.min(seats[i], i === count - 1 ? hi : seats[i + 1] - GATE_GAP);
  }
  return seats;
}

/**
 * The nearest face to this link's target — or the one it is already drawn on,
 * while that one is still in play and still nearly as close.
 */
function chooseFace(choices: readonly Ranked[], previous: Face | undefined): Face {
  const kept = previous ? choices.find((c) => c.face === previous) : undefined;
  if (kept && kept.dist <= choices[0].dist + FACE_HOLD_PX) return kept.face;
  return choices[0].face;
}

/** Gate order along one face, carried over from the last frame. */
function stableAlong(
  face: Face,
  ids: readonly string[],
  cards: ReadonlyMap<string, Point>,
  kept: readonly string[],
): string[] {
  const live = new Set(ids);
  const held = kept.filter((id) => live.has(id));
  const seen = new Set(held);
  const along = (id: string) => alongAxis(face, cards.get(id)!);
  const fresh = ids
    .filter((id) => !seen.has(id))
    .sort((a, b) => along(a) - along(b) || a.localeCompare(b));

  const order = [...held, ...fresh];
  for (let pass = 0; pass < order.length; pass += 1) {
    let swapped = false;
    for (let i = 0; i + 1 < order.length; i += 1) {
      if (along(order[i]) - along(order[i + 1]) <= SLOT_HYSTERESIS) continue;
      [order[i], order[i + 1]] = [order[i + 1], order[i]];
      swapped = true;
    }
    if (!swapped) break;
  }
  return order;
}

/** Where a target sits along a face: across the top and bottom, down the sides. */
function alongAxis(face: Face, p: Point): number {
  return face === 'left' || face === 'right' ? p.y : p.x;
}

/** `at` is a coordinate along the face: an x on the top and bottom, a y on the sides. */
function gateAt(rect: Rect, face: Face, at: number): Gate {
  switch (face) {
    case 'top':
      return { face, point: { x: at, y: rect.top }, outward: { x: 0, y: -1 } };
    case 'bottom':
      return { face, point: { x: at, y: rect.bottom }, outward: { x: 0, y: 1 } };
    case 'left':
      return { face, point: { x: rect.left, y: at }, outward: { x: -1, y: 0 } };
    default:
      return { face, point: { x: rect.right, y: at }, outward: { x: 1, y: 0 } };
  }
}

/**
 * How far the target is from a face — measured to the face itself, not to the
 * plane it lies in, so a host level with a frame's right edge reads as being
 * off its right side and not equally off its top and bottom.
 */
function faceDistance(rect: Rect, face: Face, target: Point): number {
  const x = clamp(target.x, rect.left, rect.right);
  const y = clamp(target.y, rect.top, rect.bottom);
  switch (face) {
    case 'top':
      return Math.hypot(target.x - x, target.y - rect.top);
    case 'bottom':
      return Math.hypot(target.x - x, target.y - rect.bottom);
    case 'left':
      return Math.hypot(target.x - rect.left, target.y - y);
    default:
      return Math.hypot(target.x - rect.right, target.y - y);
  }
}

/**
 * How much of the journey a face actually covers — the target's distance out
 * along that face's own normal.
 *
 * A target off a corner is *exactly* as far from the two faces that meet there,
 * by construction, and the tie used to fall to a fixed face order: a host up
 * and to the right of a stack left through the top, then had to travel the
 * length of the block sideways to reach it. Of two equidistant faces the one
 * pointing the way the wire is mostly going is the one that gets it out.
 */
function reach(rect: Rect, face: Face, target: Point): number {
  switch (face) {
    case 'top':
      return rect.top - target.y;
    case 'bottom':
      return target.y - rect.bottom;
    case 'left':
      return rect.left - target.x;
    default:
      return target.x - rect.right;
  }
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

function rectOf(frame: GateFrame): Rect {
  return {
    left: frame.x - frame.hw,
    right: frame.x + frame.hw,
    top: frame.y - frame.hh,
    bottom: frame.y + frame.hh,
  };
}
