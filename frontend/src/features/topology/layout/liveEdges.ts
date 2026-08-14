/**
 * Live orthogonal edge routes with fan-out and obstacle avoidance.
 *
 * Links must not cut through unrelated cards on the way — they detour around
 * with an orthogonal corridor, the same way a cable goes around a rack.
 */

import type { Point } from './elkLayout';
import { assignFrameGates, clearFrameGates, type Gate, type GateCrossing } from './frameGates';

const PORT = 24;
/**
 * How far a fanned wire stays clear of the corner of the card it meets.
 *
 * Slots used to be a flat `PORT` apart wherever they landed, which is fine for
 * two wires on a tall card and wrong for three on a host badge 60px deep: the
 * outer two arrived level with its corners, or past them, and a wire that
 * lands beyond the card it is drawn to reads as a wire that missed. The face a
 * link arrives on is only as wide as the card, so the fan is fitted to it.
 */
const FACE_INSET = 9;
/** Inflate cards so a hairline does not graze a border and still look wrong. */
const PAD = 14;
const MARGIN = 18;
/**
 * How much further out each wire of a bundle passes an obstacle it has to round.
 *
 * A skim is defined by the obstacle's edge, so wires that share a fan and meet
 * the same obstacle would otherwise round it on exactly the same line and arrive
 * as one thick wire that forks at the end. Wide enough to read as two wires,
 * narrow enough that a wire changing places in its fan is not a jump.
 */
const SKIM_LANE = 16;
/**
 * Quantize anchors so sub-pixel wobble does not flip corridors every frame.
 *
 * This decides *which* corridor a link takes, never where it is drawn: the
 * geometry is rebuilt from live anchors on every frame. Reusing the cached
 * polyline itself is what made wires advance in 10px steps while the cards
 * they join moved smoothly — the stutter was the quantizer leaking into the
 * picture.
 */
const STICKY_QUANT = 10;
/**
 * How much shorter a rival corridor must be before the wire actually moves to it.
 *
 * A VHV and an HVH elbow between the same two cards are the *same* Manhattan
 * length, so "pick the shortest" is a coin toss decided by the port offsets —
 * and it lands differently every time the quantizer lets the search run again.
 * That was the flicker: the card slid two pixels and the link snapped to the
 * other side of the rectangle, back and forth, for the whole drag.
 *
 * The corridor in use has to be beaten by a real margin, so a tie keeps the
 * wire where the eye last saw it. Only clearance overrules the margin — see
 * `solveRoute`.
 */
const SWITCH_MARGIN = 0.2;
const SWITCH_MARGIN_PX = 40;
/**
 * How far a card must overtake its neighbour before the two swap fan slots.
 *
 * Slot order is what spreads links across a shared card, so a swap moves both
 * wires by a whole PORT at once. Ordering on raw position re-decided that on
 * every frame near the crossing, which made the two stubs trade places over
 * and over while the card was still sliding past.
 */
const SLOT_HYSTERESIS = 26;
/** How far the other side has to lead before a card takes its links on it. */
const AXIS_HOLD = 1.25;
/**
 * How far the other axis has to lead before a wire leaves the bundle its card
 * is already taking.
 *
 * Wires meeting one card on one face turn in an ordered set of columns in front
 * of it (see `Fan.turn`), and a wire that peels off for a neighbouring face has
 * to cross all of them to reach its own. Coming in at 55° below the horizontal
 * is not a good enough reason for that — the wire is going the same way as the
 * others and belongs in the same bundle. Coming in from almost directly above
 * is: that wire crosses nothing, and forcing it round onto the crowded face
 * would be the detour.
 */
const AXIS_JOIN = 1.6;
/**
 * How far a card must eat into the corridor in use before the wire gives it up.
 *
 * Clearance is measured against a card inflated by `PAD`, so a wire "hits" a
 * card it is merely passing near. Judging the corridor already on screen by
 * that same hairline meant every card that drifted alongside a link during a
 * drag threw it onto a detour and then handed it back — a re-route each way.
 * A wire riding close to a card for a moment is nothing; a wire drawn through
 * one is the thing worth a jump, so only the second kind moves it.
 */
const HOLD_SLACK = 18;
/**
 * How much longer a corridor may be and still win for meeting a gate square —
 * see `prefer` in `solveRoute`.
 *
 * The elbows this arbitrates are the same length, so the allowance only has to
 * absorb the fan offsets. Making it generous would buy a tidy exit with a wire
 * that visibly goes the long way round, which is a worse trade.
 */
const AXIS_MARGIN_PX = 48;
/** Obstacle ids that stand for a whole assembly rather than a card. */
const FRAME_PREFIX = 'frame:';
/**
 * How far a wire runs straight on each side of a gate.
 *
 * A gate is a socket in a face, so the border has to be crossed square to it:
 * the wire turns `GATE_REACH` short of the face, goes through, and only turns
 * again `GATE_STANDOFF` outside. Without the run-up the corridor is free to
 * arrive along the border and turn out at the last moment, which draws a wire
 * riding the frame's edge — the picture the gates exist to stop.
 *
 * The standoff also has to clear `PAD`, or the leg outside would count as
 * starting inside the frame and stop treating it as solid.
 */
const GATE_REACH = 22;
const GATE_STANDOFF = PAD + 12;

/** Two points closer than this are the same point, not a bend. */
const EPS = 1e-6;

/** Which way a corridor runs where it meets its endpoint. */
type Axis = 'hvh' | 'vhv';

export interface RouteEdge {
  readonly key: string;
  readonly kind: string;
  readonly src: string;
  readonly dst: string;
}

/** Axis-aligned card (centre + half extents) that a link must not cross. */
export interface RouteObstacle {
  readonly id: string;
  readonly x: number;
  readonly y: number;
  readonly hw: number;
  readonly hh: number;
}

interface StickyEntry {
  /** Which candidate corridor won — see `Candidate.id`. */
  route: string;
  sig: string;
}

const stickyRoutes = new Map<string, StickyEntry>();
/** Fan slot order per card side, as edge keys — held across frames for hysteresis. */
const slotOrder = new Map<string, string[]>();
/** Which side of a card each link meets it on — held across frames, same reason. */
const linkSides = new Map<string, Side>();

/** Drop cached corridors (tests / full graph replace). */
/**
 * The fan each link end was given, keyed `<link key>><end>` — for the rig.
 *
 * The side a wire was *planned* to meet its card on is not always the side it
 * visibly arrives on, and telling the two apart is most of reading a bad
 * picture.
 */
export function currentFans(): ReadonlyMap<string, string> {
  return new Map(lastFans);
}

const lastFans = new Map<string, string>();

/**
 * The corridor each leg is currently drawn along, keyed `<link key><leg tag>`.
 *
 * Corridor ids name a decision — "skim the right of this frame", "escape via
 * the cluster's top-left corner" — which is what you need to read when a wire
 * takes a path that looks wrong and the coordinates alone will not say why.
 */
export function currentRoutes(): ReadonlyMap<string, string> {
  const out = new Map<string, string>();
  for (const [key, held] of stickyRoutes) out.set(key, held.route);
  return out;
}

export function clearStickyRoutes(): void {
  stickyRoutes.clear();
  slotOrder.clear();
  linkSides.clear();
  nestOrder.clear();
  lastFans.clear();
  clearFrameGates();
}

/**
 * Route every drawable edge against live anchors, spreading departures and
 * routing around intervening cards.
 */
export function routeDrawnEdges(
  edges: readonly RouteEdge[],
  anchors: ReadonlyMap<string, Point>,
  drawnKinds: ReadonlySet<string>,
  obstacles: readonly RouteObstacle[] = [],
): Map<string, Point[]> {
  const drawable: RouteEdge[] = [];
  for (const edge of edges) {
    if (!drawnKinds.has(edge.kind)) continue;
    if (!anchors.has(edge.src) || !anchors.has(edge.dst)) continue;
    if (edge.src === edge.dst) continue;
    drawable.push(edge);
  }

  const crossed = crossings(drawable, anchors, obstacles);
  const gates = assignFrameGates(
    obstacles.filter((o) => o.id.startsWith(FRAME_PREFIX)),
    [...crossed.values()].flatMap((sides) => sides.crossings),
  );

  // Gates first, because a card no longer sees the far card: it sees the gate
  // the wire comes out of, and that is what its fan has to be ordered by.
  const gated = new Map<string, { out: FrameGate | null; in: FrameGate | null }>();
  const seen = new Map<string, Seen>();
  for (const edge of drawable) {
    const sides = crossed.get(edge.key);
    const out = sides?.out ? withGate(sides.out, gates.get(gateId(edge.key, 'out'))) : null;
    const into = sides?.in ? withGate(sides.in, gates.get(gateId(edge.key, 'in'))) : null;
    gated.set(edge.key, { out, in: into });
    seen.set(edge.key, {
      atSrc: into ? reach(into, 'out') : anchors.get(edge.dst)!,
      atDst: out ? reach(out, 'out') : anchors.get(edge.src)!,
    });
  }

  const { ports, approach } = assignPorts(drawable, anchors, seen, cardsAt(anchors, obstacles));
  const out = new Map<string, Point[]>();
  const liveKeys = new Set<string>();

  for (const edge of drawable) {
    const sides = gated.get(edge.key)!;
    out.set(
      edge.key,
      routeThroughGates(
        edge,
        anchors.get(edge.src)!,
        anchors.get(edge.dst)!,
        ports.get(edge.key)!,
        sides.out,
        sides.in,
        {
          src: approach.get(endKey(edge.key, 'src')) ?? null,
          dst: approach.get(endKey(edge.key, 'dst')) ?? null,
        },
        obstacles,
        liveKeys,
      ),
    );
  }

  for (const key of [...stickyRoutes.keys()]) {
    if (!liveKeys.has(key)) stickyRoutes.delete(key);
  }

  return out;
}

/**
 * One end of a leg: where the wire meets the card, and where it turns to get
 * there.
 *
 * `off` is across the face it arrives on. `lane` nudges the middle of the
 * corridor, and is deliberately not the same number: a fan of three on a host
 * badge lands 11px apart because that is all the face there is, while their
 * approaches need a full `PORT` between them or they arrive as one thick line
 * with a fork on the end.
 *
 * `turn` is the one that makes a fan-in read as a fan-in. Wires converging on a
 * card each used to turn at the midpoint of their *own* run, which puts the
 * turns wherever the far ends happen to be — a wire from a distant stack turned
 * a long way out, a wire from the stack next door turned close in, and each one
 * then crossed the other's lane on the way to its socket. Turning them all in
 * one ordered set of columns in front of the card is what stops that; see
 * `laneDirection` for which of them gets the column nearest the card.
 */
interface Fan {
  readonly off: number;
  readonly lane: number;
  /** Where this wire turns, on the axis across its approach — null to use the midpoint. */
  readonly turn: number | null;
  /** Which axis `turn` is a coordinate on. */
  readonly turnAxis: 'x' | 'y';
  /** Which column of the fan this is, counting from the card. */
  readonly rank: number;
}

/** At a gate the face gave the wire one point, so there is nothing to fan. */
const CENTRED: Fan = { off: 0, lane: 0, turn: null, turnAxis: 'x', rank: 0 };

/**
 * How far in front of a card the nearest approach column sits.
 *
 * Room for the turn to read as a turn rather than as a kink against the card,
 * and — since this is where published wires bunch up — room for a port chip to
 * sit on the straight bit that follows.
 */
const APPROACH_GAP = 34;

/**
 * The card each endpoint is drawn on, by endpoint id.
 *
 * A folded container has no card of its own — it draws on its service — so the
 * lookup is by seat rather than by id, and an endpoint whose seat is not a card
 * (nothing to fan against) is simply absent.
 */
function cardsAt(
  anchors: ReadonlyMap<string, Point>,
  obstacles: readonly RouteObstacle[],
): Map<string, RouteObstacle> {
  const cards = obstacles.filter((o) => !o.id.startsWith(FRAME_PREFIX));
  const out = new Map<string, RouteObstacle>();
  for (const [id, at] of anchors) {
    const card =
      cards.find((o) => o.id === id) ??
      cards.find((o) => Math.abs(o.x - at.x) < 0.5 && Math.abs(o.y - at.y) < 0.5);
    if (card) out.set(id, card);
  }
  return out;
}

/** A gate together with the frame it is cut into. */
interface FrameGate {
  readonly gate: Gate;
  readonly frame: RouteObstacle;
}

function withGate(frame: RouteObstacle, gate: Gate | undefined): FrameGate | null {
  return gate ? { gate, frame } : null;
}

function gateId(key: string, side: 'out' | 'in'): string {
  return `${key}>${side}`;
}

interface EdgeCrossings {
  /** The block the link leaves, if its source sits inside one. */
  out: RouteObstacle | null;
  /** The block the link enters, if its target sits inside one. */
  in: RouteObstacle | null;
  crossings: GateCrossing[];
}

/**
 * Which links cross out of (or into) a block, and where each is headed.
 *
 * A link with both ends in the same assembly crosses nothing — it is wiring
 * inside the box, and `enclosingFrame` returns null for it on both sides.
 */
function crossings(
  edges: readonly RouteEdge[],
  anchors: ReadonlyMap<string, Point>,
  obstacles: readonly RouteObstacle[],
): Map<string, EdgeCrossings> {
  const frames = obstacles.filter((o) => o.id.startsWith(FRAME_PREFIX));
  const out = new Map<string, EdgeCrossings>();
  if (frames.length === 0) return out;

  for (const edge of edges) {
    const from = anchors.get(edge.src)!;
    const to = anchors.get(edge.dst)!;
    const leaving = enclosingFrame(frames, from, to);
    const entering = enclosingFrame(frames, to, from);
    if (!leaving && !entering) continue;

    const list: GateCrossing[] = [];
    if (leaving) {
      list.push({ id: gateId(edge.key, 'out'), frame: leaving.id, target: to, from });
    }
    if (entering) {
      list.push({ id: gateId(edge.key, 'in'), frame: entering.id, target: from, from: to });
    }
    out.set(edge.key, { out: leaving, in: entering, crossings: list });
  }
  return out;
}

/** The tightest frame holding `inside` while leaving `outside` out of it. */
function enclosingFrame(
  frames: readonly RouteObstacle[],
  inside: Point,
  outside: Point,
): RouteObstacle | null {
  let best: RouteObstacle | null = null;
  for (const frame of frames) {
    if (!covers(frame, inside) || covers(frame, outside)) continue;
    if (best === null || frame.hw * frame.hh < best.hw * best.hh) best = frame;
  }
  return best;
}

/**
 * Route one link, breaking it at the gates it passes through.
 *
 * Each leg is solved on its own — same corridor search, same stickiness — and
 * the legs are joined by the straight run that crosses the border. Splitting
 * the wire is what makes the gate binding: a single corridor from card to card
 * would meet the face wherever its elbow happened to fall.
 */
function routeThroughGates(
  edge: RouteEdge,
  from: Point,
  to: Point,
  port: Fan2,
  leaving: FrameGate | null,
  entering: FrameGate | null,
  // The side each card takes its links on — the leg that meets a card should
  // meet it that way, or the fan spreads across an axis nobody ordered it on.
  approach: { src: Axis | null; dst: Axis | null },
  obstacles: readonly RouteObstacle[],
  liveKeys: Set<string>,
): Point[] {
  const ends = new Set([edge.src, edge.dst]);
  const srcFan = port.src;
  const dstFan = port.dst;
  const single = CENTRED;

  const leg = (tag: string, a: Point, b: Point, ap: Fan, bp: Fan, prefer: Axis | null) =>
    routeLeg(
      `${edge.key}${tag}`,
      a,
      b,
      {
        srcOff: ap.off,
        dstOff: bp.off,
        laneOff: ap.lane + bp.lane,
        turnX: turnOn('x', ap, bp),
        turnY: turnOn('y', ap, bp),
        spread: (ap.rank + bp.rank) * SKIM_LANE,
      },
      obstacles,
      ends,
      liveKeys,
      prefer,
    );

  // One elbow cannot both leave one face square and meet another square when
  // the two are at right angles. Neither end gets the say then, and the
  // shortest corridor wins as it would anywhere else.
  if (!leaving && !entering) {
    return leg('', from, to, srcFan, dstFan, agree(approach.src, approach.dst));
  }

  const path: Point[] = [];
  let cursor = from;
  let cursorFan = srcFan;

  if (leaving) {
    const inner = reach(leaving, 'in');
    append(path, leg('#out', from, inner, cursorFan, single, agree(approach.src, gateAxis(leaving))));
    cursor = reach(leaving, 'out');
    cursorFan = single;
    path.push(cursor);
  }

  const arrival = entering ? reach(entering, 'out') : to;
  append(
    path,
    leg(
      '#mid',
      cursor,
      arrival,
      cursorFan,
      entering ? single : dstFan,
      agree(
        leaving ? gateAxis(leaving) : approach.src,
        entering ? gateAxis(entering) : approach.dst,
      ),
    ),
  );

  if (entering) {
    const landing = reach(entering, 'in');
    path.push(landing);
    append(path, leg('#in', landing, to, single, dstFan, agree(gateAxis(entering), approach.dst)));
  }

  return dedupe(path);
}

/** The axis a gate's run-up takes: out through a side, or up through a face. */
function gateAxis(at: FrameGate): Axis {
  return at.gate.outward.x !== 0 ? 'hvh' : 'vhv';
}

function agree(a: Axis | null, b: Axis | null): Axis | null {
  if (a === null) return b;
  if (b === null) return a;
  return a === b ? a : null;
}

/**
 * Where the run-up to a gate turns: short of the face inside, or clear of it
 * outside.
 *
 * Clamped to the frame's own half-extent so a shallow block cannot have its
 * inner turning point pushed out through the opposite face.
 */
function reach(at: FrameGate, side: 'in' | 'out'): Point {
  const { gate, frame } = at;
  const depth =
    gate.outward.x === 0
      ? Math.min(GATE_REACH, frame.hh * 0.5)
      : Math.min(GATE_REACH, frame.hw * 0.5);
  const step = side === 'out' ? GATE_STANDOFF : -depth;
  return { x: gate.point.x + gate.outward.x * step, y: gate.point.y + gate.outward.y * step };
}

function append(path: Point[], leg: readonly Point[]): void {
  for (const p of leg) {
    const prev = path[path.length - 1];
    if (prev && Math.abs(prev.x - p.x) < EPS && Math.abs(prev.y - p.y) < EPS) continue;
    path.push(p);
  }
}

/** One solved run between two points, held on the corridor it won last frame. */
function routeLeg(
  cacheKey: string,
  from: Point,
  to: Point,
  port: Porting,
  obstacles: readonly RouteObstacle[],
  ends: ReadonlySet<string>,
  liveKeys: Set<string>,
  prefer: Axis | null,
): Point[] {
  liveKeys.add(cacheKey);
  const ignore = new Set(ends);
  // A card dragged on top of another card leaves both its endpoints inside
  // that card. Nothing can route around an obstacle it starts inside, so
  // every corridor scores as blocked and the search falls back to whichever
  // is shortest this frame — which is how a wire ends up flipping sides while
  // you hold a card over a neighbour. Standing on a card means ignoring it.
  for (const o of obstacles) {
    if (ignore.has(o.id)) continue;
    if (covers(o, from) || covers(o, to)) ignore.add(o.id);
  }
  const sig = routeSignature(from, to, port, obstacles, ignore);

  // Same corridor as last frame: skip the search, but redraw the corridor at
  // the anchors the cards are actually at right now.
  const prev = stickyRoutes.get(cacheKey);
  if (prev && prev.sig === sig) {
    const live = rebuildRoute(prev.route, from, to, port, obstacles, ignore);
    if (live) return live;
  }

  const solved = solveRoute(from, to, port, obstacles, ignore, prev?.route, prefer);
  stickyRoutes.set(cacheKey, { route: solved.route, sig });
  return solved.path;
}

/** How one leg is drawn: the offsets at its ends and where it turns — see `Fan`. */
interface Porting {
  srcOff: number;
  dstOff: number;
  laneOff: number;
  /** Where the corridor turns, when a fan has asked for a column. */
  turnX?: number | null;
  turnY?: number | null;
  /**
   * How far outside an obstacle this wire passes when it has to go round one.
   *
   * Wires in the same fan meet the same obstacles, and a skim is defined by the
   * obstacle's edge, so without this they all round it on the very same line and
   * arrive as one thick wire that splits at the last moment.
   */
  spread?: number;
}

/**
 * The turn column for one leg, on one axis.
 *
 * Only an end that fans has one, and only one end of a leg ever does: the other
 * is a gate, which is a single point. A leg between two fanned cards keeps the
 * midpoint it would have had, since neither end can claim the middle.
 */
function turnOn(axis: 'x' | 'y', src: Fan, dst: Fan): number | null {
  const from = src.turnAxis === axis ? src.turn : null;
  const to = dst.turnAxis === axis ? dst.turn : null;
  if (from != null && to != null) return null;
  return from ?? to;
}

function routeSignature(
  from: Point,
  to: Point,
  port: Porting,
  obstacles: readonly RouteObstacle[],
  ignore: ReadonlySet<string>,
): string {
  const q = (n: number) => Math.round(n / STICKY_QUANT);
  const bits = [
    `${q(from.x)},${q(from.y)}>${q(to.x)},${q(to.y)}`,
    `${port.srcOff}/${port.dstOff}/${port.laneOff}/${port.spread ?? 0}`,
  ];
  for (const o of obstacles) {
    if (ignore.has(o.id)) continue;
    bits.push(`${o.id}:${q(o.x)},${q(o.y)}`);
  }
  return bits.join('|');
}

/** Where each end of a link is seen from by the card at the other end. */
interface Seen {
  readonly atSrc: Point;
  readonly atDst: Point;
}

/** One link as the card it meets sees it. */
interface Incident {
  readonly key: string;
  readonly end: 'src' | 'dst';
  /** Where the wire comes from: the far card, or the gate it comes out of. */
  readonly from: Point;
  /** The far card itself, which is what says which way the wire is travelling. */
  readonly origin: Point;
}

/** Which side of a card a wire meets it on. */
type Side = 'top' | 'right' | 'bottom' | 'left';

/**
 * Fan slots, one stack per side of each card.
 *
 * A card fed from the left and from the right has two wires that never share a
 * side and have no business sharing a stack: fanning them together pushed each
 * off centre for the other's sake, and a link between two cards sitting level
 * came out with a step in it for no reason anyone could see. A side is only
 * crowded by the wires that actually arrive on it.
 */
function assignPorts(
  edges: readonly RouteEdge[],
  anchors: ReadonlyMap<string, Point>,
  seen: ReadonlyMap<string, Seen>,
  cards: ReadonlyMap<string, RouteObstacle>,
): { ports: Map<string, Fan2>; approach: Map<string, Axis> } {
  const incident = new Map<string, Incident[]>();

  const add = (at: string, entry: Incident) => {
    const list = incident.get(at) ?? [];
    list.push(entry);
    incident.set(at, list);
  };

  for (const edge of edges) {
    const view = seen.get(edge.key)!;
    const atSrc = anchors.get(edge.dst)!;
    const atDst = anchors.get(edge.src)!;
    add(edge.src, { key: edge.key, end: 'src', from: view.atSrc, origin: atSrc });
    add(edge.dst, { key: edge.key, end: 'dst', from: view.atDst, origin: atDst });
  }

  const fanAt = new Map<string, Fan>();
  const approach = new Map<string, Axis>();
  const liveStacks = new Set<string>();
  const liveEnds = new Set<string>();

  for (const [urn, list] of incident) {
    const seat = anchors.get(urn)!;
    const bundle = bundleAxis(urn, list, seat);
    const bySide = new Map<Side, Incident[]>();
    for (const entry of list) {
      const at = endKey(entry.key, entry.end);
      liveEnds.add(at);
      const side = sideOf(`${urn}|${at}`, seat, entry.from, bundle);
      bySide.set(side, [...(bySide.get(side) ?? []), entry]);
    }

    for (const [side, group] of bySide) {
      const stack = `${urn}|${side}`;
      liveStacks.add(stack);
      const axis = sideAxis(side);
      const across: Axis = axis === 'hvh' ? 'vhv' : 'hvh';
      const order = orderingAxis(axis, group);
      const card = cards.get(urn);
      const count = group.length;
      const step = landingStep(card, side, count);
      const inward = side === 'left' || side === 'top' ? 1 : -1;
      const half = card ? (side === 'left' || side === 'right' ? card.hw : card.hh) : 0;
      const base = side === 'left' || side === 'right' ? seat.x : seat.y;
      // A bundle out of one gate arrives stacked across its face, not along it,
      // and has to be fanned past the exit rather than in front of the card.
      const beyond = order === axis ? null : beyondTheExit(group, axis, seat, card);
      const near = nestsFromTheNearSide(stack, group, order, seat);
      const nearestFirst = beyond ? !near : near;

      stableSlotOrder(stack, group, order).forEach((entry, index) => {
        const at = endKey(entry.key, entry.end);
        // How far out this wire turns, counted in columns from the card.
        const rank = nearestFirst ? index : count - 1 - index;
        fanAt.set(
          at,
          beyond
            ? {
                off: 0,
                lane: beyond(rank),
                turn: null,
                turnAxis: side === 'left' || side === 'right' ? 'x' : 'y',
                rank,
              }
            : {
                off: (index - (count - 1) / 2) * step,
                lane: ((count - 1) / 2 - rank) * PORT * inward,
                turn: count < 2 ? null : base - inward * (half + APPROACH_GAP + rank * PORT),
                turnAxis: side === 'left' || side === 'right' ? 'x' : 'y',
                rank,
              },
        );
        const fan = fanAt.get(at)!;
        lastFans.set(
          at,
          `${side}${beyond ? ' beyond' : ''} rank=${rank} off=${fan.off.toFixed(0)} lane=${fan.lane.toFixed(0)} turn=${fan.turn == null ? '-' : `${fan.turnAxis}${fan.turn.toFixed(0)}`}`,
        );
        // The corridor a wire in a stacked bundle needs is the one that crosses
        // its face, not the one that meets it head on: head on is the direction
        // every wire in the bundle is already coming from.
        approach.set(at, beyond ? across : axis);
      });
    }
  }

  for (const stack of [...slotOrder.keys()]) {
    if (!liveStacks.has(stack)) slotOrder.delete(stack);
  }
  for (const stack of [...nestOrder.keys()]) {
    if (!liveStacks.has(stack)) nestOrder.delete(stack);
  }
  for (const at of [...linkSides.keys()]) {
    if (!liveEnds.has(at.slice(at.indexOf('|') + 1))) linkSides.delete(at);
  }
  for (const at of [...lastFans.keys()]) {
    if (!liveEnds.has(at)) lastFans.delete(at);
  }

  const ports = new Map<string, Fan2>();
  for (const edge of edges) {
    ports.set(edge.key, {
      src: fanAt.get(endKey(edge.key, 'src')) ?? CENTRED,
      dst: fanAt.get(endKey(edge.key, 'dst')) ?? CENTRED,
    });
  }
  return { ports, approach };
}

/** Both ends of one link, as the offsets each meets its card with. */
interface Fan2 {
  readonly src: Fan;
  readonly dst: Fan;
}

/**
 * How far apart wires sharing one face of one card land.
 *
 * `PORT` while the face can afford it, and the face split evenly once it cannot
 * — so a fan never runs off the end of the card it belongs to. Endpoints with
 * no card behind them (a seat that is not drawn) keep the plain spacing.
 */
function landingStep(card: RouteObstacle | undefined, side: Side, count: number): number {
  if (count < 2) return 0;
  if (!card) return PORT;
  const half = side === 'left' || side === 'right' ? card.hh : card.hw;
  const span = Math.max(0, half * 2 - FACE_INSET * 2);
  return Math.min(PORT, span / (count - 1));
}

/**
 * Whether the first slot on a face is the one that turns closest to the card.
 *
 * A wire's last stretch runs from its turn into the card, across every column
 * in between. When a bundle comes at a face from one side of it — every wire
 * above the host, say, so all of them turn down — the wire landing nearest the
 * crowd has the furthest still to go, and it has to go *closest* to the card:
 * anything else and its run cuts across the columns of the wires still coming.
 * That was six crossings in front of a host dragged below its stacks.
 *
 * Slots run along the face in the order the wires come from, so "nearest the
 * crowd" is the first slot when the crowd is above and the last when it is
 * below. A bundle arriving from both sides of the face brackets it and cannot
 * cross itself, so either order will do.
 */
function nestsFromTheNearSide(
  stack: string,
  group: readonly Incident[],
  axis: Axis,
  seat: Point,
): boolean {
  const middle = alongSide(axis, seat);
  const held = nestOrder.get(stack);
  // Which side the crowd is on, judged with a margin either way: a wire level
  // with the middle of the face answers both, and reversing a whole fan is far
  // too big a move to make on a pixel of drift. A fan that brackets the face
  // cannot cross itself, so an order that is no longer earned is still kept.
  const low = group.every((entry) => alongSide(axis, entry.from) <= middle + SLOT_HYSTERESIS);
  const high = group.every((entry) => alongSide(axis, entry.from) >= middle - SLOT_HYSTERESIS);
  const nests = low === high ? (held ?? low) : low;
  nestOrder.set(stack, nests);
  return nests;
}

/** The nesting order in use on each side of each card — see above. */
const nestOrder = new Map<string, boolean>();

/**
 * How far past the gate the nearest wire of a stacked bundle turns.
 *
 * Enough to read as a turn of its own rather than a wobble on the gate's own
 * run-up, and small enough to leave the rest of the face for the wires behind
 * it.
 */
const EXIT_GAP = 20;

/**
 * Where each wire of a bundle that shares one gate turns onto its face.
 *
 * Wires converging from one gate all reach the face at the same point along it,
 * so the columns in front of the face cannot separate them: each wire's run-up
 * to the gate sweeps the whole width of the picture, and any column between the
 * gate and the card is a column some other wire in the bundle has to cross.
 * Past the exit the runs are clear, so that is where the fan goes — spread on
 * along the face in the direction the bundle is travelling, nearest wire first,
 * each turning onto the face at its own column.
 *
 * Returns the lane offsets to use, or null when there is not enough face left
 * beyond the exit to spread across and the ordinary fan is the best on offer.
 */
function beyondTheExit(
  group: readonly Incident[],
  axis: Axis,
  seat: Point,
  card: RouteObstacle | undefined,
): ((rank: number) => number) | null {
  const count = group.length;
  if (!card || count < 2) return null;

  const at = (p: Point) => alongSide(axis, p);
  const base = at(seat);
  const exit = mean(group.map((entry) => at(entry.from)));
  const travel = mean(group.map((entry) => at(entry.from) - at(entry.origin)));
  const dir = Math.sign(travel) || Math.sign(exit - base) || 1;

  // Only a bundle that arrives *across* the face it lands on has this problem:
  // its exit is level with the card, so the face has room either side of it to
  // fan into. An exit off the end of the face is a bundle coming at it along its
  // own axis, which the ordinary fan already spreads properly.
  const reach = (axis === 'hvh' ? card.hh : card.hw) - FACE_INSET;
  if (Math.abs(exit - base) > reach) return null;

  const room = reach - dir * (exit - base);
  const step = Math.min(PORT, (room - EXIT_GAP) / (count - 1));
  if (step < PORT / 2) return null;

  // The corridor's middle is halfway from the exit to the seat, so a lane
  // measured from there puts the turn at `exit + dir * …`.
  return (rank) => (exit - base) / 2 + dir * (EXIT_GAP + rank * step);
}

function mean(values: readonly number[]): number {
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

/**
 * The axis a fan's order is read off.
 *
 * Normally it is the one the face runs along: the wire coming from highest up
 * takes the highest slot on a left face. But a bundle that leaves its block
 * through one gate arrives at the same point along that face, and then the axis
 * says nothing — the order falls to whatever noise is in the positions, and the
 * columns in front of the card come out in an order that has the wires cutting
 * across one another to reach them. What tells those wires apart is how far each
 * still has to come, so that is what puts them in order.
 */
function orderingAxis(face: Axis, group: readonly Incident[]): Axis {
  const spread = (axis: Axis) => {
    const at = group.map((entry) => alongSide(axis, entry.from));
    return Math.max(...at) - Math.min(...at);
  };
  const across: Axis = face === 'hvh' ? 'vhv' : 'hvh';
  return spread(face) >= PORT || spread(across) < PORT ? face : across;
}

function endKey(key: string, end: 'src' | 'dst'): string {
  return `${key}>${end}`;
}

/** Left and right sides stack their wires downwards; top and bottom, across. */
function sideAxis(side: Side): Axis {
  return side === 'left' || side === 'right' ? 'hvh' : 'vhv';
}

/**
 * Which side of a card this wire meets it on, from where it comes from.
 *
 * A card being dragged past the diagonal sits at the crossover for a while, and
 * the side decides both the slot and the corridor, so flipping it costs the
 * wire a jump. The side in use has to be beaten by `AXIS_HOLD` — the same
 * bargain the corridors strike in `solveRoute`. Swapping left for right is not
 * hedged the same way: that takes the far end crossing clean over this card.
 *
 * `bundle` is the axis this card's other wires are on, and the wire is pulled
 * towards it (`AXIS_JOIN`) so that it does not peel off across their columns for
 * a few degrees of approach. That pull is on top of the hold, not instead of it:
 * a bundle's own membership decides the bundle, so a wire that could be talked
 * out of its side by the crowd alone would leave the crowd, be called back by
 * the crowd it just left, and flap between two faces frame after frame.
 */
function sideOf(id: string, card: Point, from: Point, bundle: Axis | null): Side {
  const dx = from.x - card.x;
  const dy = from.y - card.y;
  const held = linkSides.get(id);
  const heldAxis = held === undefined ? null : sideAxis(held);
  const favour = (axis: Axis | null, margin: number) =>
    axis === 'hvh' ? margin : axis === 'vhv' ? 1 / margin : 1;
  const lead = favour(bundle, AXIS_JOIN) * favour(heldAxis, AXIS_HOLD);
  const across = Math.abs(dx) * lead >= Math.abs(dy);

  const side: Side = across ? (dx >= 0 ? 'right' : 'left') : (dy >= 0 ? 'bottom' : 'top');
  linkSides.set(id, side);
  return side;
}

/**
 * The axis most of a card's wires meet it on, or null when they are split.
 *
 * Read from the sides the wires are actually drawn on where that is known, so
 * the answer is as steady as the picture rather than being re-derived from
 * positions that are still moving.
 */
function bundleAxis(urn: string, list: readonly Incident[], seat: Point): Axis | null {
  if (list.length < 2) return null;

  let across = 0;
  let along = 0;
  for (const entry of list) {
    const held = linkSides.get(`${urn}|${endKey(entry.key, entry.end)}`);
    const axis = held
      ? sideAxis(held)
      : Math.abs(entry.from.x - seat.x) >= Math.abs(entry.from.y - seat.y)
        ? 'hvh'
        : 'vhv';
    if (axis === 'hvh') across += 1;
    else along += 1;
  }
  if (across === along) return null;
  return across > along ? 'hvh' : 'vhv';
}

/** Where a link sits on the side it arrives on: down it, or across it. */
function alongSide(axis: Axis, p: Point): number {
  return axis === 'hvh' ? p.y : p.x;
}

/**
 * Fan slots for one side of one card, carried over from the last frame.
 *
 * Links are spread along that side in the order they arrive in:
 * the wire coming from highest up takes the highest slot, and two wires cross
 * for nothing the moment that stops being true. What counts as "coming from"
 * is the gate a wire leaves its block by, not the card behind it — a card
 * hidden deep inside an assembly says nothing about where its wire emerges.
 *
 * Re-deciding the order from scratch every frame means two links level with
 * each other trade slots on whatever noise is in their positions; the order
 * here only changes once one has genuinely overtaken the other by
 * `SLOT_HYSTERESIS`, and then it changes once.
 */
function stableSlotOrder(stack: string, list: readonly Incident[], axis: Axis): Incident[] {
  const byKey = new Map(list.map((entry) => [entry.key, entry]));
  const at = (key: string) => alongSide(axis, byKey.get(key)!.from);

  // Fresh links join in position order; everything else keeps last frame's slot.
  const kept = (slotOrder.get(stack) ?? []).filter((key) => byKey.has(key));
  const seen = new Set(kept);
  const fresh = list
    .filter((entry) => !seen.has(entry.key))
    .sort((a, b) => {
      const delta = alongSide(axis, a.from) - alongSide(axis, b.from);
      if (Math.abs(delta) > 1) return delta;
      return a.key.localeCompare(b.key);
    })
    .map((entry) => entry.key);

  const order = [...kept, ...fresh];

  // Overtaking is judged on the arrival axis alone. Falling back to the other
  // axis for a near-tie — the way the fresh sort once did — gives two rules
  // that disagree either side of the band, and a pair straddling it swaps on
  // one rule and swaps straight back on the other. Links level along the side
  // keep whatever order they were given.
  for (let pass = 0; pass < order.length; pass += 1) {
    let swapped = false;
    for (let i = 0; i + 1 < order.length; i += 1) {
      if (at(order[i]) - at(order[i + 1]) <= SLOT_HYSTERESIS) continue;
      [order[i], order[i + 1]] = [order[i + 1], order[i]];
      swapped = true;
    }
    if (!swapped) break;
  }

  slotOrder.set(stack, order);
  return order.map((key) => byKey.get(key)!);
}

/** Orthogonal elbow with lateral stubs so shared endpoints fan out. */
export function fanOrtho(
  from: Point,
  to: Point,
  srcSlot: number,
  srcCount: number,
  dstSlot: number,
  dstCount: number,
): Point[] {
  return routeOrthoAvoiding(from, to, srcSlot, srcCount, dstSlot, dstCount, [], new Set());
}

/**
 * Prefer a clear orthogonal route; fall back to the shortest candidate if every
 * corridor still clips something (dense graphs).
 */
export function routeOrthoAvoiding(
  from: Point,
  to: Point,
  srcSlot: number,
  srcCount: number,
  dstSlot: number,
  dstCount: number,
  obstacles: readonly RouteObstacle[],
  ignore: ReadonlySet<string>,
): Point[] {
  const srcOff = (srcSlot - (srcCount - 1) / 2) * PORT;
  const dstOff = (dstSlot - (dstCount - 1) / 2) * PORT;
  return solveRoute(from, to, { srcOff, dstOff, laneOff: srcOff + dstOff }, obstacles, ignore).path;
}

const DIRECT_ROUTE = 'direct';

/**
 * Search every corridor and report which one won, so it can be replayed.
 *
 * `incumbent` is the corridor already on screen. It keeps the wire unless a
 * rival is clear when it is not, or beats it by `SWITCH_MARGIN` — a corridor
 * change is a jump the eye reads as a glitch, so it has to be worth making.
 *
 * `prefer` is the axis this leg should run on where it meets a gate. VHV and
 * HVH between the same two points are the *same* Manhattan length, so without
 * it the choice falls to candidate order — and a leg that has just come out
 * through the right face of a block turns straight back up the outside of it,
 * which is the crawl along the border the gates were cut to stop.
 */
function solveRoute(
  from: Point,
  to: Point,
  port: Porting,
  obstacles: readonly RouteObstacle[],
  ignore: ReadonlySet<string>,
  incumbent?: string,
  prefer?: Axis | null,
): { path: Point[]; route: string } {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  if (Math.abs(dx) < 4 && Math.abs(dy) < 4) return { path: [from, to], route: DIRECT_ROUTE };

  const blockers = obstacles.filter((o) => !ignore.has(o.id));
  const candidates = buildCandidates(from, to, port, blockers);

  let best: Candidate | null = null;
  let bestLen = Infinity;
  let bestClear: Candidate | null = null;
  let bestClearLen = Infinity;
  let bestClearAligned = false;
  let held: Candidate | null = null;
  let heldLen = Infinity;
  let heldClear = false;

  for (const candidate of candidates) {
    const clean = dedupe(candidate.points);
    if (clean.length < 2) continue;
    const len = pathLength(clean);
    const clear = !pathHitsObstacles(clean, blockers);
    if (len < bestLen) {
      best = { id: candidate.id, points: clean };
      bestLen = len;
    }
    if (clear) {
      const aligned = prefer != null && axisOf(candidate.id) === prefer;
      // A corridor that meets its gate square is worth a detour of
      // `AXIS_MARGIN_PX`; beyond that the shorter wire is the honest one.
      const shorter = len < bestClearLen - 0.5;
      const better = aligned && !bestClearAligned && len <= bestClearLen + AXIS_MARGIN_PX;
      const worse = !aligned && bestClearAligned && bestClearLen <= len + AXIS_MARGIN_PX;
      if (bestClear === null || better || (shorter && !worse)) {
        bestClear = { id: candidate.id, points: clean };
        bestClearLen = len;
        bestClearAligned = aligned;
      }
    }
    if (candidate.id === incumbent) {
      held = { id: candidate.id, points: clean };
      heldLen = len;
      heldClear = clear || !pathHitsObstacles(clean, blockers, HOLD_SLACK);
    }
  }

  const won = bestClear ?? best;
  if (!won) return { path: [from, to], route: DIRECT_ROUTE };

  // Hold the corridor in use unless the winner is clear and it is not — a card
  // moved into its way and it genuinely has to go around — or the winner is
  // shorter by more than the margin.
  if (held && (heldClear || !bestClear)) {
    const wonLen = bestClear ? bestClearLen : bestLen;
    if (heldLen <= wonLen * (1 + SWITCH_MARGIN) + SWITCH_MARGIN_PX) {
      return { path: held.points, route: held.id };
    }
  }

  return { path: won.points, route: won.id };
}

/**
 * Redraw a corridor that already won, at the anchors of this frame.
 *
 * Returns null when the corridor no longer exists — its obstacle drifted out of
 * the pair's bounding box, say — and the caller should search again.
 */
function rebuildRoute(
  route: string,
  from: Point,
  to: Point,
  port: Porting,
  obstacles: readonly RouteObstacle[],
  ignore: ReadonlySet<string>,
): Point[] | null {
  if (route === DIRECT_ROUTE) return [from, to];
  const blockers = obstacles.filter((o) => !ignore.has(o.id));
  for (const candidate of buildCandidates(from, to, port, blockers)) {
    if (candidate.id !== route) continue;
    const clean = dedupe(candidate.points);
    return clean.length >= 2 ? clean : null;
  }
  return null;
}

/**
 * One corridor option. `id` names the *rule* that produced it — "skim above
 * card X", "escape via the cluster's bottom-left corner" — never a coordinate,
 * so the same id rebuilds the same corridor after everything has moved.
 */
interface Candidate {
  readonly id: string;
  readonly points: Point[];
}

function buildCandidates(
  from: Point,
  to: Point,
  port: Porting,
  blockers: readonly RouteObstacle[],
): Candidate[] {
  const { srcOff, dstOff } = port;
  /**
   * The lane the corridor's middle runs down.
   *
   * This used to be the two ends' offsets averaged and then damped to under
   * half a `PORT`, which put the middles of a bundle 5px apart: three wires
   * converging on one host ran down what looked like a single line and only
   * separated in the last few pixels before the card, so the picture read as
   * one wire with three prongs. A wire that lands in its own socket travels in
   * its own lane to get there.
   */
  const midOff = port.laneOff;

  const fromX = from.x + srcOff;
  const toX = to.x + dstOff;
  const fromY = from.y + srcOff;
  const toY = to.y + dstOff;
  // A fan's column only counts while it is actually between the two ends. Out
  // of range it is not a turn at all — it is a wire that leaves the run it is
  // supposed to be making, doubles back, and comes in from behind.
  const midY0 = withinRun(port.turnY, from.y, to.y) ?? (from.y + to.y) / 2 + midOff;
  const midX0 = withinRun(port.turnX, from.x, to.x) ?? (from.x + to.x) / 2 + midOff;

  const candidates: Candidate[] = [];
  const push = (id: string, points: Point[]) => candidates.push({ id, points });

  // Default VHV / HVH elbows.
  push('vhv', vhv(from, to, fromX, toX, midY0));
  push('hvh', hvh(from, to, fromY, toY, midX0));

  // Corridors that skim above / below / beside every intervening card.
  // Only a frame is skimmed by a whole bundle at once — it is the thing wires
  // have to file past — and only there is the extra clearance worth the wire
  // moving when it changes places in its fan.
  const spread = port.spread ?? 0;
  const lane = (o: RouteObstacle) => MARGIN + (o.id.startsWith(FRAME_PREFIX) ? spread : 0);
  const between = blockers.filter((o) => inCorridor(o, from, to));
  for (const o of between) {
    const r = inflate(o);
    const clear = lane(o);
    push(`vhv:above:${o.id}`, vhv(from, to, fromX, toX, r.top - clear));
    push(`vhv:below:${o.id}`, vhv(from, to, fromX, toX, r.bottom + clear));
    push(`hvh:left:${o.id}`, hvh(from, to, fromY, toY, r.left - clear));
    push(`hvh:right:${o.id}`, hvh(from, to, fromY, toY, r.right + clear));
  }

  // One big detour around the whole obstacle cluster.
  if (between.length > 0) {
    let left = Infinity;
    let right = -Infinity;
    let top = Infinity;
    let bottom = -Infinity;
    for (const o of between) {
      const r = inflate(o);
      left = Math.min(left, r.left);
      right = Math.max(right, r.right);
      top = Math.min(top, r.top);
      bottom = Math.max(bottom, r.bottom);
    }
    const wide = MARGIN + (between.some((o) => o.id.startsWith(FRAME_PREFIX)) ? spread : 0);
    push('vhv:above:*', vhv(from, to, fromX, toX, top - wide));
    push('vhv:below:*', vhv(from, to, fromX, toX, bottom + wide));
    push('hvh:left:*', hvh(from, to, fromY, toY, left - wide));
    push('hvh:right:*', hvh(from, to, fromY, toY, right + wide));

    // Two-bend escape via a free corner (outside the cluster).
    const escapeX = [left - wide, right + wide];
    const escapeY = [top - wide, bottom + wide];
    for (let ix = 0; ix < escapeX.length; ix += 1) {
      for (let iy = 0; iy < escapeY.length; iy += 1) {
        const ex = escapeX[ix];
        const ey = escapeY[iy];
        push(`corner:v:${ix}${iy}`, [
          from,
          { x: fromX, y: from.y },
          { x: fromX, y: ey },
          { x: ex, y: ey },
          { x: toX, y: ey },
          { x: toX, y: to.y },
          to,
        ]);
        push(`corner:h:${ix}${iy}`, [
          from,
          { x: from.x, y: fromY },
          { x: ex, y: fromY },
          { x: ex, y: ey },
          { x: ex, y: toY },
          { x: to.x, y: toY },
          to,
        ]);
      }
    }
  }

  return candidates;
}

/**
 * The elbows keep their full shape even when the two ends are nearly lined up.
 *
 * Collapsing a near-aligned elbow to a straight run looks like the tidy thing
 * to do, and it is where the wire stays collapsed. But the two forms are not
 * the same wire: one runs at the near end's offset the whole way, the other
 * splits the difference in the middle. A card jiggling across the threshold
 * flips between them, and the wire changes shape under a hand that is holding
 * still. Left whole, the jog just shrinks to nothing as the ends line up, and
 * the collinear points it leaves behind draw as the straight line they are.
 */
function vhv(from: Point, to: Point, fromX: number, toX: number, midY: number): Point[] {
  return dedupe([
    from,
    { x: fromX, y: from.y },
    { x: fromX, y: midY },
    { x: toX, y: midY },
    { x: toX, y: to.y },
    to,
  ]);
}

/** As `vhv`, turned through a right angle. */
function hvh(from: Point, to: Point, fromY: number, toY: number, midX: number): Point[] {
  return dedupe([
    from,
    { x: from.x, y: fromY },
    { x: midX, y: fromY },
    { x: midX, y: toY },
    { x: to.x, y: toY },
    to,
  ]);
}

/** `at`, if it lies between the two ends with room to turn on either side. */
function withinRun(at: number | null | undefined, a: number, b: number): number | null {
  if (at == null) return null;
  const lo = Math.min(a, b) + MARGIN;
  const hi = Math.max(a, b) - MARGIN;
  return at >= lo && at <= hi ? at : null;
}

function inCorridor(o: RouteObstacle, from: Point, to: Point): boolean {
  const minX = Math.min(from.x, to.x) - o.hw - PAD;
  const maxX = Math.max(from.x, to.x) + o.hw + PAD;
  const minY = Math.min(from.y, to.y) - o.hh - PAD;
  const maxY = Math.max(from.y, to.y) + o.hh + PAD;
  return o.x >= minX && o.x <= maxX && o.y >= minY && o.y <= maxY;
}

/** True when the point sits inside the card's inflated rectangle. */
function covers(o: RouteObstacle, p: Point): boolean {
  const r = inflate(o);
  return p.x > r.left && p.x < r.right && p.y > r.top && p.y < r.bottom;
}

function inflate(
  o: RouteObstacle,
  slack = 0,
): {
  left: number;
  right: number;
  top: number;
  bottom: number;
} {
  const padX = Math.max(0, o.hw + PAD - slack);
  const padY = Math.max(0, o.hh + PAD - slack);
  return {
    left: o.x - padX,
    right: o.x + padX,
    top: o.y - padY,
    bottom: o.y + padY,
  };
}

/**
 * True if any segment of the polyline crosses an obstacle interior.
 *
 * `slack` shrinks every card before the test, for asking the softer question
 * "is this wire actually drawn *through* something" — see `HOLD_SLACK`.
 */
export function pathHitsObstacles(
  path: readonly Point[],
  obstacles: readonly RouteObstacle[],
  slack = 0,
): boolean {
  for (let i = 1; i < path.length; i += 1) {
    if (segmentHits(path[i - 1], path[i], obstacles, slack)) return true;
  }
  return false;
}

function segmentHits(
  a: Point,
  b: Point,
  obstacles: readonly RouteObstacle[],
  slack: number,
): boolean {
  for (const o of obstacles) {
    const r = inflate(o, slack);
    if (segmentHitsRect(a, b, r)) return true;
  }
  return false;
}

function segmentHitsRect(
  a: Point,
  b: Point,
  r: { left: number; right: number; top: number; bottom: number },
): boolean {
  const minX = Math.min(a.x, b.x);
  const maxX = Math.max(a.x, b.x);
  const minY = Math.min(a.y, b.y);
  const maxY = Math.max(a.y, b.y);

  // Degenerate / point.
  if (maxX - minX < 0.5 && maxY - minY < 0.5) {
    return a.x > r.left && a.x < r.right && a.y > r.top && a.y < r.bottom;
  }

  // Horizontal
  if (Math.abs(a.y - b.y) < 0.5) {
    const y = a.y;
    if (y <= r.top || y >= r.bottom) return false;
    return maxX > r.left && minX < r.right;
  }

  // Vertical
  if (Math.abs(a.x - b.x) < 0.5) {
    const x = a.x;
    if (x <= r.left || x >= r.right) return false;
    return maxY > r.top && minY < r.bottom;
  }

  // Should not happen for ortho paths — treat as hit if bbox overlaps.
  return maxX > r.left && minX < r.right && maxY > r.top && minY < r.bottom;
}

/**
 * Which axis a corridor sets off along — and, every candidate being symmetric,
 * the axis it lands on too. Read from the id.
 *
 * The id names the rule, not the geometry, so this stays true after everything
 * has moved — which is the whole reason corridors are identified by rule.
 */
function axisOf(id: string): Axis {
  if (id.startsWith('hvh') || id.startsWith('corner:h')) return 'hvh';
  return 'vhv';
}

function pathLength(path: readonly Point[]): number {
  let len = 0;
  for (let i = 1; i < path.length; i += 1) {
    len += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
  }
  return len;
}

/**
 * Drop points the elbow builders repeated, and nothing else.
 *
 * This used to merge anything within half a pixel, which cost twice over. A
 * bend that drifted inside the tolerance vanished and came back as the card it
 * belonged to moved, so the wire changed shape under a still hand; and merging
 * two points that were a fraction apart on *both* axes left a segment that was
 * neither horizontal nor vertical, which `segmentHitsRect` has no case for and
 * reads as a hit. A vertex a fraction of a pixel from its neighbour is drawn as
 * a plain corner anyway — see `strokeRoundedOrtho` — so there is nothing to
 * gain by removing it.
 */
function dedupe(points: Point[]): Point[] {
  const out: Point[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.x - p.x) < EPS && Math.abs(prev.y - p.y) < EPS) continue;
    out.push(p);
  }
  return out.length >= 2 ? out : points;
}
