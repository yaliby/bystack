/**
 * Live orthogonal edge routes with fan-out and obstacle avoidance.
 *
 * Links must not cut through unrelated cards on the way — they detour around
 * with an orthogonal corridor, the same way a cable goes around a rack.
 */

import type { Point } from './elkLayout';

const PORT = 24;
/** Inflate cards so a hairline does not graze a border and still look wrong. */
const PAD = 14;
const MARGIN = 18;
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
/** Fan slot order per card, as edge keys — held across frames for hysteresis. */
const slotOrder = new Map<string, string[]>();

/** Drop cached corridors (tests / full graph replace). */
export function clearStickyRoutes(): void {
  stickyRoutes.clear();
  slotOrder.clear();
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

  const ports = assignPorts(drawable, anchors);
  const out = new Map<string, Point[]>();
  const liveKeys = new Set<string>();

  for (const edge of drawable) {
    liveKeys.add(edge.key);
    const from = anchors.get(edge.src)!;
    const to = anchors.get(edge.dst)!;
    const port = ports.get(edge.key)!;
    const ignore = new Set([edge.src, edge.dst]);
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
    const prev = stickyRoutes.get(edge.key);
    if (prev && prev.sig === sig) {
      const live = rebuildRoute(prev.route, from, to, port, obstacles, ignore);
      if (live) {
        out.set(edge.key, live);
        continue;
      }
    }

    const solved = solveRoute(from, to, port, obstacles, ignore, prev?.route);
    stickyRoutes.set(edge.key, { route: solved.route, sig });
    out.set(edge.key, solved.path);
  }

  for (const key of [...stickyRoutes.keys()]) {
    if (!liveKeys.has(key)) stickyRoutes.delete(key);
  }

  return out;
}

interface Porting {
  srcSlot: number;
  srcCount: number;
  dstSlot: number;
  dstCount: number;
}

function routeSignature(
  from: Point,
  to: Point,
  port: Porting,
  obstacles: readonly RouteObstacle[],
  ignore: ReadonlySet<string>,
): string {
  const q = (n: number) => Math.round(n / STICKY_QUANT);
  const bits = [`${q(from.x)},${q(from.y)}>${q(to.x)},${q(to.y)}`, `${port.srcSlot}/${port.dstSlot}`];
  for (const o of obstacles) {
    if (ignore.has(o.id)) continue;
    bits.push(`${o.id}:${q(o.x)},${q(o.y)}`);
  }
  return bits.join('|');
}

function assignPorts(
  edges: readonly RouteEdge[],
  anchors: ReadonlyMap<string, Point>,
): Map<string, Porting> {
  const incident = new Map<string, { key: string; other: string }[]>();

  const add = (at: string, key: string, other: string) => {
    const list = incident.get(at) ?? [];
    list.push({ key, other });
    incident.set(at, list);
  };

  for (const edge of edges) {
    add(edge.src, edge.key, edge.dst);
    add(edge.dst, edge.key, edge.src);
  }

  const slotAt = new Map<string, Map<string, number>>();
  const countAt = new Map<string, number>();

  for (const [urn, list] of incident) {
    const sorted = stableSlotOrder(urn, list, anchors);
    countAt.set(urn, sorted.length);
    const slots = new Map<string, number>();
    sorted.forEach((entry, index) => slots.set(entry.key, index));
    slotAt.set(urn, slots);
  }

  for (const urn of [...slotOrder.keys()]) {
    if (!incident.has(urn)) slotOrder.delete(urn);
  }

  const out = new Map<string, Porting>();
  for (const edge of edges) {
    out.set(edge.key, {
      srcSlot: slotAt.get(edge.src)?.get(edge.key) ?? 0,
      srcCount: countAt.get(edge.src) ?? 1,
      dstSlot: slotAt.get(edge.dst)?.get(edge.key) ?? 0,
      dstCount: countAt.get(edge.dst) ?? 1,
    });
  }
  return out;
}

/**
 * Fan slots for one card, carried over from the last frame.
 *
 * Links arriving at a card are spread left-to-right by where they come from.
 * Re-deciding that from scratch every frame means two neighbours sitting at
 * the same x trade slots on whatever noise is in their positions; the order
 * here only changes once a card has genuinely overtaken its neighbour by
 * `SLOT_HYSTERESIS`, and then it changes once.
 */
function stableSlotOrder(
  urn: string,
  list: readonly { key: string; other: string }[],
  anchors: ReadonlyMap<string, Point>,
): { key: string; other: string }[] {
  const byKey = new Map(list.map((entry) => [entry.key, entry]));

  // Fresh links join in position order; everything else keeps last frame's slot.
  const kept = (slotOrder.get(urn) ?? []).filter((key) => byKey.has(key));
  const seen = new Set(kept);
  const fresh = list
    .filter((entry) => !seen.has(entry.key))
    .sort((a, b) => {
      const pa = anchors.get(a.other)!;
      const pb = anchors.get(b.other)!;
      if (Math.abs(pa.x - pb.x) > 1) return pa.x - pb.x;
      if (Math.abs(pa.y - pb.y) > 1) return pa.y - pb.y;
      return a.key.localeCompare(b.key);
    })
    .map((entry) => entry.key);

  const order = [...kept, ...fresh];

  // Overtaking is judged on x alone. Falling back to y for a near-tie in x —
  // the way the fresh sort does — gives two rules that disagree either side of
  // the band, and a pair straddling it swaps on one rule and swaps straight
  // back on the other. Cards level in x keep whatever order they were given.
  const overtakes = (aKey: string, bKey: string): boolean => {
    const pa = anchors.get(byKey.get(aKey)!.other)!;
    const pb = anchors.get(byKey.get(bKey)!.other)!;
    return pa.x - pb.x > SLOT_HYSTERESIS;
  };

  for (let pass = 0; pass < order.length; pass += 1) {
    let swapped = false;
    for (let i = 0; i + 1 < order.length; i += 1) {
      if (!overtakes(order[i], order[i + 1])) continue;
      [order[i], order[i + 1]] = [order[i + 1], order[i]];
      swapped = true;
    }
    if (!swapped) break;
  }

  slotOrder.set(urn, order);
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
  return solveRoute(from, to, { srcSlot, srcCount, dstSlot, dstCount }, obstacles, ignore).path;
}

const DIRECT_ROUTE = 'direct';

/**
 * Search every corridor and report which one won, so it can be replayed.
 *
 * `incumbent` is the corridor already on screen. It keeps the wire unless a
 * rival is clear when it is not, or beats it by `SWITCH_MARGIN` — a corridor
 * change is a jump the eye reads as a glitch, so it has to be worth making.
 */
function solveRoute(
  from: Point,
  to: Point,
  port: Porting,
  obstacles: readonly RouteObstacle[],
  ignore: ReadonlySet<string>,
  incumbent?: string,
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
    if (clear && len < bestClearLen) {
      bestClear = { id: candidate.id, points: clean };
      bestClearLen = len;
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
  const srcOff = (port.srcSlot - (port.srcCount - 1) / 2) * PORT;
  const dstOff = (port.dstSlot - (port.dstCount - 1) / 2) * PORT;
  const midOff =
    ((port.srcSlot + port.dstSlot) / 2 - (port.srcCount + port.dstCount - 2) / 4) * (PORT * 0.45);

  const fromX = from.x + srcOff;
  const toX = to.x + dstOff;
  const fromY = from.y + srcOff;
  const toY = to.y + dstOff;
  const midY0 = (from.y + to.y) / 2 + midOff;
  const midX0 = (from.x + to.x) / 2 + midOff;

  const candidates: Candidate[] = [];
  const push = (id: string, points: Point[]) => candidates.push({ id, points });

  // Default VHV / HVH elbows.
  push('vhv', vhv(from, to, fromX, toX, midY0));
  push('hvh', hvh(from, to, fromY, toY, midX0));

  // Corridors that skim above / below / beside every intervening card.
  const between = blockers.filter((o) => inCorridor(o, from, to));
  for (const o of between) {
    const r = inflate(o);
    push(`vhv:above:${o.id}`, vhv(from, to, fromX, toX, r.top - MARGIN));
    push(`vhv:below:${o.id}`, vhv(from, to, fromX, toX, r.bottom + MARGIN));
    push(`hvh:left:${o.id}`, hvh(from, to, fromY, toY, r.left - MARGIN));
    push(`hvh:right:${o.id}`, hvh(from, to, fromY, toY, r.right + MARGIN));
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
    push('vhv:above:*', vhv(from, to, fromX, toX, top - MARGIN));
    push('vhv:below:*', vhv(from, to, fromX, toX, bottom + MARGIN));
    push('hvh:left:*', hvh(from, to, fromY, toY, left - MARGIN));
    push('hvh:right:*', hvh(from, to, fromY, toY, right + MARGIN));

    // Two-bend escape via a free corner (outside the cluster).
    const escapeX = [left - MARGIN, right + MARGIN];
    const escapeY = [top - MARGIN, bottom + MARGIN];
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

function vhv(from: Point, to: Point, fromX: number, toX: number, midY: number): Point[] {
  if (Math.abs(fromX - toX) < 1.5) {
    return dedupe([from, { x: fromX, y: from.y }, { x: fromX, y: to.y }, to]);
  }
  return dedupe([
    from,
    { x: fromX, y: from.y },
    { x: fromX, y: midY },
    { x: toX, y: midY },
    { x: toX, y: to.y },
    to,
  ]);
}

function hvh(from: Point, to: Point, fromY: number, toY: number, midX: number): Point[] {
  if (Math.abs(fromY - toY) < 1.5) {
    return dedupe([from, { x: from.x, y: fromY }, { x: to.x, y: fromY }, to]);
  }
  return dedupe([
    from,
    { x: from.x, y: fromY },
    { x: midX, y: fromY },
    { x: midX, y: toY },
    { x: to.x, y: toY },
    to,
  ]);
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

function pathLength(path: readonly Point[]): number {
  let len = 0;
  for (let i = 1; i < path.length; i += 1) {
    len += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
  }
  return len;
}

function dedupe(points: Point[]): Point[] {
  const out: Point[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.x - p.x) < 0.5 && Math.abs(prev.y - p.y) < 0.5) continue;
    out.push(p);
  }
  return out.length >= 2 ? out : points;
}
