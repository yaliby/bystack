/**
 * Where the `:8443 → 8443` chip on a published link goes.
 *
 * A port number is only worth drawing if it can be read, and read *as part of
 * its own wire*. That rules out three placements at once: on the stroke (the
 * dashes cut through the digits), on a card or a stack frame (the number then
 * reads as belonging to that assembly), and — the failure this replaces —
 * anywhere far enough away that the eye cannot tell which wire it labels.
 *
 * So the chip is never positioned absolutely. Every candidate is a point *on
 * the path*, pushed one short step off the stroke, and the search picks the
 * cheapest of them. Because the offsets are bounded, the worst case is a chip
 * grazing a frame — never a chip adrift in the canvas, and never a chip that
 * was not drawn at all.
 */

import type { Point } from '../layout/elkLayout';

export interface Rect {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
}

export interface PortLabelRequest {
  /** Edge key — carries the chip's seat across frames. */
  readonly key: string;
  /** The route this chip labels, in world coordinates. */
  readonly path: readonly Point[];
  /** Measured chip width (text + padding). */
  readonly width: number;
}

export interface PortLabelPlacement {
  /** Centre of the chip. */
  readonly at: Point;
  /** True when the chip sits in open canvas, clear of everything. */
  readonly clear: boolean;
}

/** A placement plus the bookkeeping that carries it to the next frame. */
interface Seated extends PortLabelPlacement {
  readonly seat: Seat;
}

/** Gap between the stroke and the near edge of the chip. */
const LIFT = 7;
/**
 * Extra distances from the wire, each dearer than the last (`W_LIFT`).
 *
 * The ladder is short and it stops: drop the host card in among the services
 * and there is no clear ground within reach of the wire, and a search allowed
 * to keep stepping outward answers that by putting the number in the middle of
 * nowhere. Better a chip on the tint two rungs out than one that has left.
 */
const LIFT_STEPS = [0, 11, 22, 34, 46];
/** How finely the path is sampled for anchors. */
const SAMPLE_STEP = 10;
/** Endpoints carry the link's terminal dot — do not label on top of one. */
const END_GAP = 26;

/**
 * Cost weights.
 *
 * These four are charged per unit of *area*, so they are worth orders of
 * magnitude more than the preferences below them and effectively forbid a
 * placement rather than discourage one. That ordering is the point: a chip
 * grazing a card by a few pixels used to be cheaper than a chip a little
 * further from the host, which is how numbers ended up parked on the host card
 * with their own wire nowhere near them.
 *
 * A frame is much cheaper to sit on than a card, because the two failures are
 * not the same size. A card is opaque and carries the service name and image —
 * a chip on one buries text under text. A frame is a tinted outline with an
 * empty middle, so a chip inside one is merely in the wrong room, and still
 * perfectly readable. Drag the host in among the stacks and there is no clear
 * spot left anywhere near the wire; given only bad options, the label belongs
 * on the tint rather than across a card's subtitle.
 */
const W_CARD = 20;
const W_FRAME = 2;
const W_CHIP = 25;
const W_WIRE = 400;
/** Preferences — these only order placements that are already in the clear. */
const W_LIFT = 3;
const W_VERTICAL = 250;
const W_TOWARD_END = 0.25;
/**
 * Chips repel each other well before they touch.
 *
 * Links converging on one host arrive as a bundle of near-parallel wires, so
 * "somewhere legal, as near the host as possible" puts every chip in the same
 * few square inches — three numbers in a stack, none of them obviously joined
 * to any particular line. Pushing them apart while they are still merely close
 * spreads them back along their own wires, which is what makes a fan-in
 * readable.
 */
const NEAR_CHIP = 74;
const W_NEAR_CHIP = 9;
/**
 * What the seat a chip already occupies is worth in the scoring.
 *
 * Without it a card drifting alongside a wire during a drag flips the chip
 * from one side of the stroke to the other and back — a 29px jump per frame
 * while nothing on screen moved more than one. Same bargain the router strikes
 * for corridors: a tie keeps the label where the eye last saw it, and only a
 * real obstruction (charged by area, so worth thousands) can move it.
 */
const HOLD_BONUS = 700;
/**
 * How far a card must eat into the seat in use before the chip gives it up.
 *
 * Blocked boxes carry a clearance band, so a chip "hits" a card it is only
 * sitting near — and a band-deep graze is worth hundreds once it is charged by
 * area, which was enough to throw the label to the other side of its wire and
 * hand it back a few frames later. The seat on screen is judged against the
 * card itself; drifting close for a moment is nothing, being covered is not.
 */
const HOLD_SLACK = 13;
/**
 * A pull toward where this chip was drawn last frame.
 *
 * Seats are matched by arc length from the host, which holds while the route
 * keeps its shape — and stops meaning anything the moment the router picks a
 * different corridor and re-parameterises the whole path. That is exactly when
 * the label was jumping furthest (131px in one frame while dragging the host).
 *
 * Distance from the last position costs the same for every candidate when the
 * wire itself has moved, so this cannot fight a chip travelling with its link;
 * it only breaks ties between candidates on a wire that has stayed put.
 */
const W_STAY = 5;
const STAY_CAP = 160;
/** How far along the wire a seat may shift and still count as the same seat. */
const SEAT_DRIFT = 28;
/**
 * How far a chip may walk back toward its best seat in one frame.
 *
 * Holding a seat and keeping the best seat are different jobs, and a hold that
 * never lets go does the first at the cost of the second: shove the graph about
 * for a while and every number stays parked wherever the shoving left it. The
 * memory is not a lock — once the picture has stopped moving, the seat creeps
 * home this far a frame, which converges without the jump the hold exists to
 * prevent.
 */
const RELAX = 9;
/**
 * How much better the best seat must be before the chip bothers walking.
 *
 * The seat in hand is priced with its clearance forgiven (`HOLD_SLACK`), so a
 * chip merely sitting near a card reads as good as one in the open and stays
 * where it is — a card sliding past is not a reason to go anywhere.
 */
const STAY_MARGIN = 1;
/**
 * How long the unconstrained best seat must sit still before the chip walks.
 *
 * Walking toward a moving target is just flicker under another name: the hand
 * is still on a card, the ideal is sliding with it, and a chip that chases it
 * is a chip that will not stay put. The walk starts only once the answer has
 * stopped changing — the difference between "I am dragging" and "I let go".
 */
const SETTLE = 12;
/** World-space jitter that still counts as the same answer. */
const QUIET_PX = 0.5;
/**
 * How long a chip goes on wanting a seat it cannot walk to before it jumps.
 *
 * Not every better seat can be reached on foot: it may be across the stroke, or
 * the stretch of wire in between may be buried under cards. Counted only after
 * the picture has settled, so a hand swinging the answer about cannot trigger
 * it — and a settled picture that the walk cannot finish is worth one jump.
 */
const PATIENCE = 45;

/**
 * Place every chip, in order, each one avoiding those already placed.
 *
 * `cards` and `frames` are both already inflated by whatever clearance the
 * caller wants, and are kept apart because they are not equally bad to land on
 * — see `W_FRAME`. `wires` is every drawn route: a chip lying across an
 * unrelated link is as unreadable as one on a card.
 */
export function placePortLabels(
  requests: readonly PortLabelRequest[],
  cards: readonly Rect[],
  frames: readonly Rect[],
  wires: readonly (readonly Point[])[],
  height: number,
): PortLabelPlacement[] {
  const placed: Rect[] = [];
  const out: PortLabelPlacement[] = [];

  for (const request of requests) {
    const { at, clear, seat } = bestPlacement(request, cards, frames, wires, placed, height);
    seats.set(request.key, seat);
    out.push({ at, clear });
    placed.push(boxAround(at, request.width, height));
  }

  const live = new Set(requests.map((request) => request.key));
  for (const key of [...seats.keys()]) {
    if (!live.has(key)) seats.delete(key);
  }
  for (const key of [...impatience.keys()]) {
    if (!live.has(key)) impatience.delete(key);
  }
  for (const key of [...quiet.keys()]) {
    if (!live.has(key)) quiet.delete(key);
  }
  return out;
}

/**
 * Which seat a chip took, named by rule rather than by coordinate.
 *
 * `fromEnd` is an arc length measured back from the host, so the same seat
 * rebuilds to the same place on the wire after everything has moved — the chip
 * slides with its link instead of being re-decided against it.
 */
interface Seat {
  readonly fromEnd: number;
  readonly side: number;
  readonly step: number;
  /** Where it was actually drawn, for the stay-put pull. */
  readonly at: Point;
}

const seats = new Map<string, Seat>();

/** A chip asking for a seat it is not getting any closer to. */
interface Waiting {
  readonly frames: number;
  /** Where along the wire it was when it started asking. */
  readonly fromEnd: number;
}

const impatience = new Map<string, Waiting>();

/** How long the unconstrained best has been in the same place. */
interface Quiet {
  readonly frames: number;
  readonly at: Point;
}

const quiet = new Map<string, Quiet>();

/** Drop remembered seats (tests / full graph replace). */
export function clearPortLabelSeats(): void {
  seats.clear();
  impatience.clear();
  quiet.clear();
}

/**
 * A placement under consideration, priced by everything that does not depend on
 * where this chip was drawn last frame.
 *
 * `blocking` is charged by area and so effectively forbids a placement;
 * `preference` only sorts placements that are already legible. Keeping the two
 * apart is what lets the walk home tell "my seat is worse" from "my seat is
 * wrong": the first is worth a slow slide, the second an immediate move.
 */
interface Candidate {
  readonly at: Point;
  readonly side: number;
  readonly step: number;
  readonly fromEnd: number;
  readonly blocking: number;
  readonly preference: number;
}

function bestPlacement(
  request: PortLabelRequest,
  cards: readonly Rect[],
  frames: readonly Rect[],
  wires: readonly (readonly Point[])[],
  placed: readonly Rect[],
  height: number,
): Seated {
  const board = { cards, frames, wires, placed, width: request.width, height };
  const candidates = price(request, board);
  if (candidates.length === 0) {
    const tail = request.path[request.path.length - 1] ?? { x: 0, y: 0 };
    const at = { x: tail.x, y: tail.y - height };
    return { at, clear: false, seat: { fromEnd: 0, side: 1, step: 0, at } };
  }

  let home = candidates[0];
  for (const candidate of candidates) {
    if (candidate.blocking + candidate.preference < home.blocking + home.preference) {
      home = candidate;
    }
  }

  const stored = seats.get(request.key);
  if (!stored) return seated(home);

  // The seat the chip is holding this frame: last frame's, one step closer to
  // the one it would rather have.
  const aim = stepToward(request.key, stored, home, candidates, board);

  let best = home;
  let bestCost = Infinity;
  for (const candidate of candidates) {
    const held = sameSeat(candidate, aim);
    const cost =
      (held ? blocking(candidate.at, board, HOLD_SLACK) : candidate.blocking) +
      candidate.preference +
      Math.min(Math.hypot(candidate.at.x - aim.at.x, candidate.at.y - aim.at.y), STAY_CAP) * W_STAY -
      (held ? HOLD_BONUS : 0);
    if (cost < bestCost) {
      bestCost = cost;
      best = candidate;
    }
  }
  return seated(best);
}

function seated(candidate: Candidate): Seated {
  const { at, side, step, fromEnd } = candidate;
  return { at, clear: candidate.blocking === 0, seat: { fromEnd, side, step, at } };
}

/** Everything the chip could legally be asked to do on this path, priced. */
function price(request: PortLabelRequest, board: Board): Candidate[] {
  const anchors = sampleAnchors(request.path, [...board.cards, ...board.frames]);
  const out: Candidate[] = [];

  for (const anchor of anchors) {
    // The chip is offset along the segment's normal, and it has to clear the
    // stroke by its own half-extent in that direction — a chip merely centred
    // one lift away from a vertical wire still has the wire through its middle.
    const vertical = anchor.normal.x !== 0;
    const reach = (vertical ? request.width : board.height) / 2 + LIFT;

    for (const step of LIFT_STEPS) {
      for (const side of [1, -1]) {
        const at = {
          x: anchor.at.x + anchor.normal.x * (reach + step) * side,
          y: anchor.at.y + anchor.normal.y * (reach + step) * side,
        };
        const box = boxAround(at, board.width, board.height);
        let preference = step * W_LIFT + (vertical ? W_VERTICAL : 0) + anchor.fromEnd * W_TOWARD_END;
        for (const rect of board.placed) {
          preference += Math.max(0, NEAR_CHIP - gapBetween(box, rect)) * W_NEAR_CHIP;
        }
        out.push({
          at,
          side,
          step,
          fromEnd: anchor.fromEnd,
          blocking: blocking(at, board, 0),
          preference,
        });
      }
    }
  }
  return out;
}

interface Board {
  readonly cards: readonly Rect[];
  readonly frames: readonly Rect[];
  readonly wires: readonly (readonly Point[])[];
  readonly placed: readonly Rect[];
  readonly width: number;
  readonly height: number;
}

/** The things that make a chip unreadable where it lands, charged by area. */
function blocking(at: Point, board: Board, slack: number): number {
  const box = boxAround(at, board.width, board.height);
  let sum = 0;
  for (const rect of board.cards) sum += overlapArea(box, deflate(rect, slack)) * W_CARD;
  for (const rect of board.frames) sum += overlapArea(box, deflate(rect, slack)) * W_FRAME;
  for (const rect of board.placed) sum += overlapArea(box, deflate(rect, slack)) * W_CHIP;
  for (const wire of board.wires) sum += crossings(box, wire) * W_WIRE;
  return sum;
}

/**
 * Is this the seat that was held last frame?
 *
 * Matched on the rule — which side, how far out, roughly where along the wire —
 * rather than on an exact arc length, because a re-routed corridor renumbers the
 * whole path and an exact match then silently fails. That is precisely the frame
 * the label used to flip sides on, since losing the match also loses the
 * discount that was holding it still.
 */
function sameSeat(candidate: Candidate, seat: Seat): boolean {
  return (
    candidate.side === seat.side &&
    candidate.step === seat.step &&
    Math.abs(candidate.fromEnd - seat.fromEnd) <= SEAT_DRIFT
  );
}

/**
 * The seat to hold this frame: the one in hand, nudged toward the best one
 * once the picture has stopped moving.
 *
 * The nudge is what makes the memory temporary. It is bounded, so the chip
 * slides rather than jumps; it is judged against the seat in hand *with its
 * clearance forgiven*, so a card drifting alongside does not start a walk; and
 * it does not start at all while the unconstrained best is still sliding —
 * that is a drag, and chasing it is flicker.
 */
function stepToward(
  key: string,
  stored: Seat,
  home: Candidate,
  candidates: Candidate[],
  board: Board,
): Seat {
  const inHand = held(stored, candidates);
  // Nothing on this path answers to the seat any more: the wire was re-cut out
  // from under it, and there is no seat to keep.
  if (!inHand) {
    impatience.delete(key);
    quiet.delete(key);
    return { ...home, at: home.at };
  }

  const kept = blocking(inHand.at, board, HOLD_SLACK) + inHand.preference;
  const gain = kept - (home.blocking + home.preference);
  // Nothing to go to: the seat in hand is as good as the best one going.
  if (gain <= STAY_MARGIN) {
    impatience.delete(key);
    quiet.delete(key);
    return stored;
  }

  // The hand is still moving a block: hold. The walk is for afterwards.
  if (!settled(key, home)) {
    impatience.delete(key);
    return stored;
  }

  const waited = impatience.get(key);
  const walking = waited == null || Math.abs(stored.fromEnd - waited.fromEnd) > RELAX;
  const frames = walking ? 1 : waited.frames + 1;
  // Asking for the same unreachable seat, frame after frame, with the chip no
  // further along than when it started: the picture has stopped moving and the
  // walk has stopped working, so stop waiting.
  if (frames >= PATIENCE) {
    impatience.delete(key);
    return { ...home, at: home.at };
  }
  impatience.set(key, { frames, fromEnd: walking ? stored.fromEnd : waited.fromEnd });
  return alongWire(stored, home, candidates, stored.side);
}

/**
 * One step of the walk, kept on the wire.
 *
 * The seat's own place is an arc length, and the pull that carries the chip to
 * it is a distance — so the point this aims at has to be the offset track's
 * point at that arc length, not a straight line drawn to the destination. Where
 * the wire turns a corner between here and there, the straight line leaves the
 * wire, and the nearest spot to it is the one the chip is already in: the label
 * then sits still while the seat it is chasing runs away up the corridor.
 */
function alongWire(stored: Seat, home: Candidate, candidates: Candidate[], side: number): Seat {
  const seat = {
    side,
    step: rungToward(stored.step, home.step),
    fromEnd: toward(stored.fromEnd, home.fromEnd),
  };
  let at = home.at;
  let nearest = Infinity;
  for (const candidate of candidates) {
    if (candidate.side !== seat.side || candidate.step !== seat.step) continue;
    const away = Math.abs(candidate.fromEnd - seat.fromEnd);
    if (away < nearest) {
      nearest = away;
      at = candidate.at;
    }
  }
  return { ...seat, at };
}

/** The candidate that best answers to a remembered seat, if any still does. */
function held(seat: Seat, candidates: Candidate[]): Candidate | null {
  let best: Candidate | null = null;
  let nearest = Infinity;
  for (const candidate of candidates) {
    if (!sameSeat(candidate, seat)) continue;
    const away = Math.hypot(candidate.at.x - seat.at.x, candidate.at.y - seat.at.y);
    if (away < nearest) {
      nearest = away;
      best = candidate;
    }
  }
  return best;
}

/** Has the unconstrained best stopped moving? Counted in consecutive frames. */
function settled(key: string, home: Candidate): boolean {
  const was = quiet.get(key);
  const moved = was ? Math.hypot(home.at.x - was.at.x, home.at.y - was.at.y) : Infinity;
  if (moved > QUIET_PX) {
    quiet.set(key, { frames: 0, at: home.at });
    return false;
  }
  const frames = (was?.frames ?? 0) + 1;
  quiet.set(key, { frames, at: home.at });
  return frames >= SETTLE;
}

function toward(from: number, to: number): number {
  return from + Math.max(-RELAX, Math.min(RELAX, to - from));
}

/** One rung of the lift ladder at a time, so the chip drifts in rather than hops. */
function rungToward(from: number, to: number): number {
  const at = LIFT_STEPS.indexOf(from);
  const want = LIFT_STEPS.indexOf(to);
  if (at < 0 || want < 0) return to;
  return LIFT_STEPS[at + Math.sign(want - at)];
}

interface Anchor {
  readonly at: Point;
  /** Unit normal of the segment this anchor sits on. */
  readonly normal: Point;
  /** Distance back along the path from the host end. */
  readonly fromEnd: number;
}

/**
 * Points along the path, walked back from the host end.
 *
 * Measuring from the end rather than as a fraction keeps a chip the same
 * distance from the host card whether its link crosses the canvas or hops to
 * the stack next door, which is what makes a fan-in read as a fan-in.
 *
 * Anchors under a card or a frame are dropped. A route ends at the node's
 * *centre*, so its last stretch runs beneath the card and is painted over;
 * hanging a chip off that stretch put the number next to the host with no
 * visible wire reaching it — a label with nothing to belong to. A chip may
 * only be pinned where its own wire can be seen.
 */
function sampleAnchors(path: readonly Point[], blocked: readonly Rect[]): Anchor[] {
  const out: Anchor[] = [];
  const hidden: Anchor[] = [];
  let fromEnd = 0;

  for (let i = path.length - 1; i > 0; i -= 1) {
    const b = path[i];
    const a = path[i - 1];
    const dx = a.x - b.x;
    const dy = a.y - b.y;
    const length = Math.hypot(dx, dy);
    if (length < 1) continue;

    const ux = dx / length;
    const uy = dy / length;
    // Normal of the segment; the sign is applied by the caller.
    const normal = { x: -uy, y: ux };

    for (let d = SAMPLE_STEP; d < length; d += SAMPLE_STEP) {
      const along = fromEnd + d;
      if (along < END_GAP) continue;
      // Leave the far endpoint alone too — that end is the service card.
      if (i === 1 && length - d < END_GAP) break;
      const anchor = { at: { x: b.x + ux * d, y: b.y + uy * d }, normal, fromEnd: along };
      (blocked.some((rect) => contains(rect, anchor.at)) ? hidden : out).push(anchor);
    }
    fromEnd += length;
  }

  // Every visible stretch was too short to sample — better a chip on a covered
  // run than no chip at all.
  if (out.length === 0 && hidden.length > 0) return hidden;

  if (out.length === 0 && path.length >= 2) {
    const a = path[path.length - 2];
    const b = path[path.length - 1];
    const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    out.push({
      at: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      normal: { x: -(b.y - a.y) / length, y: (b.x - a.x) / length },
      fromEnd: length / 2,
    });
  }
  return out;
}

function boxAround(at: Point, width: number, height: number): Rect {
  return {
    left: at.x - width / 2,
    right: at.x + width / 2,
    top: at.y - height / 2,
    bottom: at.y + height / 2,
  };
}

function contains(r: Rect, p: Point): boolean {
  return p.x > r.left && p.x < r.right && p.y > r.top && p.y < r.bottom;
}

function overlapArea(a: Rect, b: Rect): number {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return w > 0 && h > 0 ? w * h : 0;
}

/** Shrink a box on every side, never past its own centre. */
function deflate(r: Rect, by: number): Rect {
  if (by <= 0) return r;
  const x = (r.left + r.right) / 2;
  const y = (r.top + r.bottom) / 2;
  return {
    left: Math.min(r.left + by, x),
    right: Math.max(r.right - by, x),
    top: Math.min(r.top + by, y),
    bottom: Math.max(r.bottom - by, y),
  };
}

/** Edge-to-edge distance between two boxes; 0 when they touch or overlap. */
function gapBetween(a: Rect, b: Rect): number {
  const dx = Math.max(0, Math.max(a.left, b.left) - Math.min(a.right, b.right));
  const dy = Math.max(0, Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom));
  return Math.hypot(dx, dy);
}

/** How many segments of `wire` run through the box. */
function crossings(box: Rect, wire: readonly Point[]): number {
  let hits = 0;
  for (let i = 1; i < wire.length; i += 1) {
    if (segmentHitsRect(wire[i - 1], wire[i], box)) hits += 1;
  }
  return hits;
}

function segmentHitsRect(a: Point, b: Point, r: Rect): boolean {
  const minX = Math.min(a.x, b.x);
  const maxX = Math.max(a.x, b.x);
  const minY = Math.min(a.y, b.y);
  const maxY = Math.max(a.y, b.y);
  if (maxX <= r.left || minX >= r.right || maxY <= r.top || minY >= r.bottom) return false;

  if (Math.abs(a.y - b.y) < 0.5 || Math.abs(a.x - b.x) < 0.5) return true;
  // Diagonals do not occur on ortho routes; a bbox answer is close enough.
  return true;
}
