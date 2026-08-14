/**
 * Wire lab — drives the real layout/routing modules through the Vite dev server,
 * dumps scene geometry, scores it, and renders an SVG/PNG for eyeballing.
 *
 * Not part of the app: a throwaway rig for the link-routing work.
 */

import { chromium } from 'playwright-core';
import { writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const CHROME = `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`;
const URL = 'http://localhost:5173/?mock=1';

const PAGE = async ({ moves, steps, passes, back }) => {
  const [prep, elk, live, mock, geom, theme, chips] = await Promise.all([
    import('/src/features/topology/layout/prepareTopology.ts'),
    import('/src/features/topology/layout/elkLayout.ts'),
    import('/src/features/topology/layout/liveEdges.ts'),
    import('/src/mock/demoSnapshot.ts'),
    import('/src/features/topology/layout/geometry.ts'),
    import('/src/features/topology/ui/theme.ts'),
    import('/src/features/topology/ui/portLabels.ts'),
  ]);

  const DRAWN = new Set(['depends_on', 'mounts', 'exposed_on']);
  const nodes = mock.DEMO_SNAPSHOT.nodes;
  const edges = mock.DEMO_SNAPSHOT.edges;
  const byUrn = new Map(nodes.map((n) => [n.urn, n]));
  const prepared = prep.prepareTopologyGraph(nodes, edges);
  const laid = await elk.layoutPrepared(prepared);

  const positions = new Map(laid.positions);
  const groups = new Map(laid.groupBounds);

  const find = (needle) => {
    for (const n of nodes) if (n.urn.includes(needle)) return n.urn;
    throw new Error(`no node ${needle}`);
  };

  // Moves are how the rig "drags a block": cards move alone, a stack takes its
  // members with it.
  const shift = (move, dx, dy) => {
    const urn = find(move.at);
    if (groups.has(urn)) {
      const box = groups.get(urn);
      groups.set(urn, { ...box, x: box.x + dx, y: box.y + dy });
      for (const member of prepared.stackMembers.get(urn) ?? []) {
        const p = positions.get(member.urn);
        if (p) positions.set(member.urn, { x: p.x + dx, y: p.y + dy });
      }
    } else {
      const p = positions.get(urn);
      positions.set(urn, { x: p.x + dx, y: p.y + dy });
    }
  };

  // Same folding render.ts does: a container draws on its service card.
  const realized = new Map();
  for (const edge of edges) {
    if (edge.kind !== 'realized_by') continue;
    const c = byUrn.get(edge.dst);
    if (c?.kind === 'container') realized.set(edge.src, c);
  }
  const folded = new Set([...realized.values()].map((c) => c.urn));

  const build = () => {
    const anchors = new Map(positions);
    for (const [service, container] of realized) {
      const seat = positions.get(service);
      if (seat) anchors.set(container.urn, seat);
    }

    const cards = [];
    const obstacles = [];
    for (const node of nodes) {
      if (node.kind === 'stack' || node.kind === 'image' || node.kind === 'network') continue;
      if (folded.has(node.urn)) continue;
      const pos = positions.get(node.urn);
      if (!pos) continue;
      const [hw, hh] = geom.sizeOf(node.kind);
      obstacles.push({ id: node.urn, x: pos.x, y: pos.y, hw, hh });
      const container = realized.get(node.urn);
      cards.push({
        urn: node.urn,
        kind: node.kind,
        name: node.name,
        sub: container ? theme.cardSubtitle(container) : theme.cardSubtitle(node),
        ports: container ? theme.cardPorts(container) : theme.cardPorts(node),
        x: pos.x,
        y: pos.y,
        hw,
        hh,
      });
    }
    const frames = [];
    for (const [urn, box] of groups) {
      obstacles.push({
        id: `frame:${urn}`,
        x: box.x + box.width / 2,
        y: box.y + box.height / 2,
        hw: box.width / 2,
        hh: box.height / 2,
      });
      frames.push({ urn, name: byUrn.get(urn)?.name ?? urn, ...box });
    }
    return { anchors, cards, obstacles, frames };
  };

  // Chip placement wants text widths; a mono estimate is close enough for the rig.
  const width = (text) => text.length * 5.72 + 12;
  const CLEAR = 12;

  /**
   * The point on its own wire that a chip is pinned to.
   *
   * Absolute movement cannot tell flicker from a label riding a wire that is
   * itself being dragged across the screen — and riding is the whole point. What
   * the chip does *relative to this point* is the part that is the placer's
   * doing, so that is what gets measured.
   */
  const onWire = (at, path) => {
    let best = at;
    let nearest = Infinity;
    for (let i = 1; i < path.length; i += 1) {
      const a = path[i - 1];
      const b = path[i];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy);
      if (len < 1) continue;
      const t = Math.max(0, Math.min(len, ((at.x - a.x) * dx + (at.y - a.y) * dy) / len));
      const on = { x: a.x + (dx / len) * t, y: a.y + (dy / len) * t };
      const away = Math.hypot(at.x - on.x, at.y - on.y);
      if (away < nearest) {
        nearest = away;
        best = on;
      }
    }
    return best;
  };

  /**
   * One rendered frame: route, then place the chips against that routing.
   *
   * Chips are re-placed every frame in the app and they remember where they
   * were, so a rig that only places them once at the end cannot see a label
   * that got stuck somewhere during the drag.
   */
  const frame = () => {
    const { anchors, cards, obstacles, frames } = build();
    const routed = live.routeDrawnEdges(edges, anchors, DRAWN, obstacles);

    const wires = [];
    for (const edge of edges) {
      if (!DRAWN.has(edge.kind)) continue;
      const path = routed.get(edge.key);
      if (!path) continue;
      wires.push({
        key: edge.key,
        kind: edge.kind,
        src: edge.src,
        dst: edge.dst,
        srcName: byUrn.get(edge.src)?.name ?? edge.src,
        dstName: byUrn.get(edge.dst)?.name ?? edge.dst,
        label: edge.kind === 'exposed_on' ? theme.edgePortLabel(edge) : null,
        path: path.map((p) => ({ x: p.x, y: p.y })),
      });
    }

    const chipCards = cards.map((c) => ({
      left: c.x - c.hw - CLEAR,
      right: c.x + c.hw + CLEAR,
      top: c.y - c.hh - CLEAR,
      bottom: c.y + c.hh + CLEAR,
    }));
    const chipFrames = frames.map((f) => ({
      left: f.x - CLEAR,
      right: f.x + f.width + CLEAR,
      top: f.y - CLEAR,
      bottom: f.y + f.height + CLEAR,
    }));
    const labelled = wires.filter((w) => w.label);
    const request = labelled.map((w) => ({ key: w.key, path: w.path, width: width(w.label) }));
    const place = () =>
      chips
        .placePortLabels(request, chipCards, chipFrames, wires.map((w) => w.path), 16)
        .map((p, i) => ({
          key: labelled[i].key,
          text: labelled[i].label,
          width: width(labelled[i].label),
          at: p.at,
          seat: onWire(p.at, labelled[i].path),
        }));

    return { cards, frames, obstacles, wires, place };
  };

  // The worst one-frame move each chip makes. A chip rides its own wire, so
  // some of this is the drag itself; the number is for comparing one build of
  // the placer against another on the same drag, not an absolute.
  const jumped = new Map();
  // Frames where a chip moved far enough that the eye would catch it as a
  // separate event rather than as the label riding its wire. One of these at a
  // re-route is fair; a scattering of them is flicker.
  const STARTLE = 24;
  const startled = new Map();
  const before = new Map();
  // Startles split by phase: while the hand is moving a block, and afterwards.
  // The second kind is the one nobody can excuse.
  let dragging = true;
  const still = { startles: 0, worst: 0 };
  const watch = (placement) => {
    for (const chip of placement) {
      const was = before.get(chip.key);
      if (was) {
        const moved = Math.hypot(
          chip.at.x - was.at.x - (chip.seat.x - was.seat.x),
          chip.at.y - was.at.y - (chip.seat.y - was.seat.y),
        );
        jumped.set(chip.key, Math.max(jumped.get(chip.key) ?? 0, moved));
        if (moved > STARTLE) startled.set(chip.key, (startled.get(chip.key) ?? 0) + 1);
        if (!dragging) {
          still.worst = Math.max(still.worst, moved);
          if (moved > STARTLE) still.startles += 1;
        }
      }
      before.set(chip.key, chip);
    }
    return placement;
  };

  live.clearStickyRoutes();
  chips.clearPortLabelSeats();
  // A drag is many frames, and the router is sticky on purpose: routing the
  // move in one jump can settle somewhere a hand could never have taken it.
  const legs = Math.max(1, steps ?? 1);
  const path = back ? [1, -1] : [1];
  for (const way of path) {
    for (let step = 1; step <= legs; step += 1) {
      for (const move of moves ?? []) shift(move, (move.dx / legs) * way, (move.dy / legs) * way);
      watch(frame().place());
    }
  }

  dragging = false;
  // Settling passes: a picture that keeps changing while nothing moves is a
  // bug in its own right, so the rig can ask for more than one.
  let last = frame();
  let portChips = watch(last.place());
  for (let extra = 1; extra < (passes ?? 1); extra += 1) {
    last = frame();
    portChips = watch(last.place());
  }

  // Where the chips would go with no memory of the drag. Anything the sticky
  // pass keeps away from this is a label that did not find its way home.
  chips.clearPortLabelSeats();
  const idealChips = last.place();

  const corridors = [...live.currentRoutes()].map(([leg, id]) => ({ leg, id }));
  const fans = [...live.currentFans()].map(([end, plan]) => ({ end, plan }));

  return {
    cards: last.cards,
    frames: last.frames,
    wires: last.wires,
    obstacles: last.obstacles,
    portChips,
    idealChips,
    chipJumps: [...jumped].map(([key, worst]) => ({
      key,
      worst,
      startles: startled.get(key) ?? 0,
    })),
    chipsWhenStill: still,
    corridors,
    fans,
  };
};

/* ---------------------------------------------------------------- scoring */

function segments(path) {
  const out = [];
  for (let i = 1; i < path.length; i += 1) out.push([path[i - 1], path[i]]);
  return out;
}

function crossPoint(a, b) {
  const [a1, a2] = a;
  const [b1, b2] = b;
  const aH = Math.abs(a1.y - a2.y) < 0.5;
  const bH = Math.abs(b1.y - b2.y) < 0.5;
  if (aH === bH) return null;
  const [h, v] = aH ? [a, b] : [b, a];
  const y = h[0].y;
  const x = v[0].x;
  const inX = x > Math.min(h[0].x, h[1].x) + 1 && x < Math.max(h[0].x, h[1].x) - 1;
  const inY = y > Math.min(v[0].y, v[1].y) + 1 && y < Math.max(v[0].y, v[1].y) - 1;
  return inX && inY ? { x, y } : null;
}

/**
 * Wire-on-wire crossings the eye can actually see.
 *
 * A route ends at a card's centre, so every bundle converging on one card
 * crosses under it — those are painted over and are not the picture's problem.
 */
function wireCrossings(wires, cards) {
  const hits = [];
  for (let i = 0; i < wires.length; i += 1) {
    for (let j = i + 1; j < wires.length; j += 1) {
      const a = wires[i];
      const b = wires[j];
      for (const sa of segments(a.path)) {
        for (const sb of segments(b.path)) {
          const at = crossPoint(sa, sb);
          if (!at || hidden(at, cards)) continue;
          hits.push({ a: label(a), b: label(b), at });
        }
      }
    }
  }
  return hits;
}

function hidden(at, cards) {
  return cards.some(
    (c) => Math.abs(at.x - c.x) < c.hw - 1 && Math.abs(at.y - c.y) < c.hh - 1,
  );
}

/**
 * Runs where two different wires lie on top of each other, or so close that
 * they read as one line. This is what a shared trunk looks like numerically.
 */
const MERGE_PX = 12;

function wireOverlaps(wires, cards) {
  const out = [];
  for (let i = 0; i < wires.length; i += 1) {
    for (let j = i + 1; j < wires.length; j += 1) {
      let worst = 0;
      let where = null;
      let gapAt = 0;
      for (const sa of segments(wires[i].path)) {
        for (const sb of segments(wires[j].path)) {
          const run = parallelRun(sa, sb);
          if (!run || run.length <= worst) continue;
          if (hidden(run.at, cards)) continue;
          worst = run.length;
          where = run.at;
          gapAt = run.gap;
        }
      }
      if (worst >= 40) {
        out.push({
          a: label(wires[i]),
          b: label(wires[j]),
          run: Math.round(worst),
          gap: +gapAt.toFixed(1),
          at: where,
        });
      }
    }
  }
  return out.sort((x, y) => y.run - x.run);
}

function parallelRun(a, b) {
  const aH = Math.abs(a[0].y - a[1].y) < 0.5;
  const bH = Math.abs(b[0].y - b[1].y) < 0.5;
  const aV = Math.abs(a[0].x - a[1].x) < 0.5;
  const bV = Math.abs(b[0].x - b[1].x) < 0.5;

  if (aH && bH) {
    const gap = Math.abs(a[0].y - b[0].y);
    if (gap > MERGE_PX) return null;
    const lo = Math.max(Math.min(a[0].x, a[1].x), Math.min(b[0].x, b[1].x));
    const hi = Math.min(Math.max(a[0].x, a[1].x), Math.max(b[0].x, b[1].x));
    return hi - lo > 0 ? { length: hi - lo, gap, at: { x: (lo + hi) / 2, y: a[0].y } } : null;
  }
  if (aV && bV) {
    const gap = Math.abs(a[0].x - b[0].x);
    if (gap > MERGE_PX) return null;
    const lo = Math.max(Math.min(a[0].y, a[1].y), Math.min(b[0].y, b[1].y));
    const hi = Math.min(Math.max(a[0].y, a[1].y), Math.max(b[0].y, b[1].y));
    return hi - lo > 0 ? { length: hi - lo, gap, at: { x: a[0].x, y: (lo + hi) / 2 } } : null;
  }
  return null;
}

function label(w) {
  return `${w.srcName}→${w.dstName}${w.label ? ` (${w.label})` : ''}`;
}

function manhattan(path) {
  const a = path[0];
  const b = path[path.length - 1];
  return Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
}

function pathLength(path) {
  let len = 0;
  for (let i = 1; i < path.length; i += 1) {
    len += Math.abs(path[i].x - path[i - 1].x) + Math.abs(path[i].y - path[i - 1].y);
  }
  return len;
}

function bends(path) {
  let n = 0;
  for (let i = 1; i + 1 < path.length; i += 1) {
    const dx1 = Math.abs(path[i].x - path[i - 1].x) > 0.5;
    const dx2 = Math.abs(path[i + 1].x - path[i].x) > 0.5;
    if (dx1 !== dx2) n += 1;
  }
  return n;
}

/**
 * Where each wire is last seen before it disappears under a card — the point
 * the eye reads as its socket — plus which face that is.
 */
function landings(scene, name) {
  const card = scene.cards.find((c) => c.name === name);
  if (!card) return [];
  const out = [];
  for (const w of scene.wires) {
    const inward =
      w.dstName === name ? [...w.path].reverse() : w.srcName === name ? w.path : null;
    if (!inward) continue;
    const at = boundaryPoint(inward, card);
    const side =
      Math.abs(at.x - card.x) > card.hw - 1.5
        ? at.x < card.x
          ? 'left'
          : 'right'
        : at.y < card.y
          ? 'top'
          : 'bottom';
    out.push({ wire: label(w), side, at });
  }
  return out.sort((a, b) => a.at.y - b.at.y || a.at.x - b.at.x);
}

/** Walk out from the card centre until the path leaves the card. */
function boundaryPoint(inward, card) {
  for (let i = 1; i < inward.length; i += 1) {
    const p = inward[i];
    if (Math.abs(p.x - card.x) > card.hw || Math.abs(p.y - card.y) > card.hh) {
      const a = inward[i - 1];
      if (Math.abs(a.y - p.y) < 0.5) {
        return { x: card.x + Math.sign(p.x - card.x) * card.hw, y: a.y };
      }
      return { x: a.x, y: card.y + Math.sign(p.y - card.y) * card.hh };
    }
  }
  return inward[inward.length - 1];
}

/**
 * Do the wires arriving on one face keep the order of where they came from?
 * A pair out of order is a crossing waiting to happen.
 */
function inversions(land, wires) {
  const source = new Map();
  for (const w of wires) source.set(label(w), w.path[0]);
  let bad = 0;
  const pairs = [];
  for (const side of ['left', 'right', 'top', 'bottom']) {
    const group = land.filter((l) => l.side === side);
    for (let i = 0; i < group.length; i += 1) {
      for (let j = i + 1; j < group.length; j += 1) {
        const a = group[i];
        const b = group[j];
        const across = side === 'left' || side === 'right';
        const landDelta = across ? a.at.y - b.at.y : a.at.x - b.at.x;
        const fromA = source.get(a.wire);
        const fromB = source.get(b.wire);
        const fromDelta = across ? fromA.y - fromB.y : fromA.x - fromB.x;
        if (landDelta * fromDelta < 0 && Math.abs(fromDelta) > 30) {
          bad += 1;
          pairs.push(`${a.wire} / ${b.wire}`);
        }
      }
    }
  }
  return { count: bad, pairs };
}

function score(scene) {
  const crossings = wireCrossings(scene.wires, scene.cards);
  const overlaps = wireOverlaps(scene.wires, scene.cards);
  const detours = scene.wires.map((w) => ({
    wire: label(w),
    len: Math.round(pathLength(w.path)),
    direct: Math.round(manhattan(w.path)),
    ratio: +(pathLength(w.path) / Math.max(1, manhattan(w.path))).toFixed(2),
    bends: bends(w.path),
  }));
  const host = landings(scene, 'lowserv');
  return {
    crossings,
    overlaps,
    detours,
    host,
    inversions: inversions(host, scene.wires),
    chips: chipDrift(scene),
  };
}

/**
 * How far each chip ended up from where the same picture would put it fresh.
 *
 * The label is deliberately sticky — it must not flit about while a card slides
 * past — but stickiness that never lets go is its own bug: mess with the graph
 * for a while and the numbers stay parked wherever the mess left them.
 */
function chipDrift(scene) {
  const ideal = new Map((scene.idealChips ?? []).map((c) => [c.key, c.at]));
  const jumps = new Map((scene.chipJumps ?? []).map((j) => [j.key, j]));
  return (scene.portChips ?? [])
    .map((chip) => {
      const home = ideal.get(chip.key) ?? chip.at;
      return {
        text: chip.text,
        drift: Math.round(Math.hypot(chip.at.x - home.x, chip.at.y - home.y)),
        jump: Math.round(jumps.get(chip.key)?.worst ?? 0),
        startles: jumps.get(chip.key)?.startles ?? 0,
        at: chip.at,
        home,
      };
    })
    .sort((a, b) => b.drift - a.drift);
}

/* ---------------------------------------------------------------- drawing */

function svg(scene, meta) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const grow = (x, y) => {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  };
  for (const c of scene.cards) {
    grow(c.x - c.hw, c.y - c.hh);
    grow(c.x + c.hw, c.y + c.hh);
  }
  for (const f of scene.frames) {
    grow(f.x, f.y);
    grow(f.x + f.width, f.y + f.height);
  }
  for (const w of scene.wires) for (const p of w.path) grow(p.x, p.y);
  const pad = 40;
  const w = maxX - minX + pad * 2;
  const h = maxY - minY + pad * 2 + 70;
  const T = (p) => `${(p.x - minX + pad).toFixed(1)},${(p.y - minY + pad + 70).toFixed(1)}`;
  const X = (x) => (x - minX + pad).toFixed(1);
  const Y = (y) => (y - minY + pad + 70).toFixed(1);

  const parts = [];
  parts.push(`<rect width="${w}" height="${h}" fill="#0b0d12"/>`);
  parts.push(
    `<text x="${pad}" y="30" fill="#e6e8ef" font-family="monospace" font-size="18">${meta.title}</text>`,
  );
  const bad = meta.crossings + meta.overlaps + meta.inversions;
  parts.push(
    `<text x="${pad}" y="54" fill="${bad ? '#ff6b6b' : '#4ade80'}" font-family="monospace" font-size="14">crossings: ${meta.crossings}   merged runs: ${meta.overlaps}   out-of-order landings: ${meta.inversions}   worst detour: ${meta.worst}   chip drift: ${meta.drift}px</text>`,
  );

  for (const f of scene.frames) {
    const accent = f.name === 'monitoring' ? '#8b5cf6' : '#ec4899';
    parts.push(
      `<rect x="${X(f.x)}" y="${Y(f.y)}" width="${f.width}" height="${f.height}" rx="16" fill="${accent}1a" stroke="${accent}" stroke-width="1.8"/>`,
      `<text x="${X(f.x + 20)}" y="${Y(f.y + 28)}" fill="${accent}" font-family="monospace" font-size="11">${f.name.toUpperCase()}</text>`,
    );
  }

  for (const wire of scene.wires) {
    const dash = wire.kind === 'exposed_on' ? ' stroke-dasharray="7 5"' : '';
    const color =
      wire.kind === 'exposed_on' ? '#7d94ae' : wire.kind === 'mounts' ? '#b07b02' : '#a75ddd';
    parts.push(
      `<polyline points="${wire.path.map(T).join(' ')}" fill="none" stroke="${color}" stroke-width="2"${dash}/>`,
    );
  }

  for (const c of scene.cards) {
    const fill = c.kind === 'volume' ? '#151a24' : '#171c27';
    parts.push(
      `<rect x="${X(c.x - c.hw)}" y="${Y(c.y - c.hh)}" width="${c.hw * 2}" height="${c.hh * 2}" rx="10" fill="${fill}" stroke="#3b4557"/>`,
      `<text x="${X(c.x - c.hw + 14)}" y="${Y(c.y - c.hh + 18)}" fill="#e6e8ef" font-family="sans-serif" font-size="13" font-weight="600">${c.name.slice(0, 18)}</text>`,
      `<text x="${X(c.x - c.hw + 14)}" y="${Y(c.y - c.hh + 33)}" fill="#8a93a6" font-family="monospace" font-size="10">${(c.sub ?? '').slice(0, 24)}</text>`,
    );
    if (c.ports) {
      parts.push(
        `<text x="${X(c.x - c.hw + 14)}" y="${Y(c.y - c.hh + 50)}" fill="#aab3c5" font-family="monospace" font-size="9.5">${c.ports}</text>`,
      );
    }
  }

  for (const chip of scene.idealChips ?? []) {
    parts.push(
      `<rect x="${X(chip.at.x - chip.width / 2)}" y="${Y(chip.at.y - 8)}" width="${chip.width}" height="16" rx="4" fill="none" stroke="#4ade80" stroke-width="1" stroke-dasharray="3 3"/>`,
    );
  }

  for (const chip of scene.portChips) {
    parts.push(
      `<rect x="${X(chip.at.x - chip.width / 2)}" y="${Y(chip.at.y - 8)}" width="${chip.width}" height="16" rx="4" fill="#171c27" stroke="#7d94ae"/>`,
      `<text x="${X(chip.at.x - chip.width / 2 + 6)}" y="${Y(chip.at.y + 4)}" fill="#aab3c5" font-family="monospace" font-size="9.5">${chip.text}</text>`,
    );
  }

  for (const hit of meta.hits) {
    parts.push(
      `<circle cx="${X(hit.at.x)}" cy="${Y(hit.at.y)}" r="7" fill="none" stroke="#ff3b3b" stroke-width="2"/>`,
    );
  }
  for (const run of meta.merged) {
    parts.push(
      `<circle cx="${X(run.at.x)}" cy="${Y(run.at.y)}" r="11" fill="none" stroke="#ffb020" stroke-width="2" stroke-dasharray="3 3"/>`,
    );
  }
  for (const l of meta.land) {
    parts.push(
      `<circle cx="${X(l.at.x)}" cy="${Y(l.at.y)}" r="3.2" fill="#4ade80"/>`,
    );
  }

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${parts.join('')}</svg>`;
}

/* ------------------------------------------------------------------- main */

const HOST = 'host:e1';
const MON = 'stack:e1/monitoring';
const BYS = 'stack:e1/bystack';
const GRAFANA = 'service:e1/monitoring/grafana';
const PROM = 'service:e1/monitoring/prometheus';

/** `steps` drags the move in that many frames instead of teleporting. */
const SCENARIOS = {
  base: {},
  'host-up': { moves: [{ at: HOST, dx: 0, dy: -180 }] },
  'host-down': { moves: [{ at: HOST, dx: 0, dy: 200 }] },
  'host-left': { moves: [{ at: HOST, dx: -900, dy: 0 }] },
  'host-far-right': { moves: [{ at: HOST, dx: 320, dy: 0 }] },
  'mon-down': { moves: [{ at: MON, dx: 0, dy: 260 }] },
  'bys-up': { moves: [{ at: BYS, dx: 0, dy: -300 }] },
  'grafana-swap': {
    moves: [
      { at: GRAFANA, dx: 0, dy: 150 },
      { at: PROM, dx: 0, dy: -150 },
    ],
  },
  'host-into-stack': { moves: [{ at: HOST, dx: -430, dy: 120 }] },
  'host-above-all': { moves: [{ at: HOST, dx: -300, dy: -420 }] },
  'host-above-all-settled': { moves: [{ at: HOST, dx: -300, dy: -420 }], passes: 6 },
  'drag-host-above-all': { moves: [{ at: HOST, dx: -300, dy: -420 }], steps: 40 },
  'drag-host-up': { moves: [{ at: HOST, dx: 0, dy: -180 }], steps: 24 },
  'drag-host-down': { moves: [{ at: HOST, dx: 0, dy: 200 }], steps: 24 },
  'drag-host-left': { moves: [{ at: HOST, dx: -900, dy: 0 }], steps: 40 },
  'drag-mon-down': { moves: [{ at: MON, dx: 0, dy: 260 }], steps: 30 },
  'drag-mon-up': { moves: [{ at: MON, dx: 0, dy: -420 }], steps: 40 },
  'drag-swap': {
    moves: [
      { at: GRAFANA, dx: 0, dy: 150 },
      { at: PROM, dx: 0, dy: -150 },
    ],
    steps: 20,
  },
  // Round trips: drag a block away, drag it back to where it started. The
  // picture is the `base` picture again, so anything the chips are still
  // carrying is memory of the trip rather than a fact about the graph.
  'round-host-above': { moves: [{ at: HOST, dx: -300, dy: -420 }], steps: 30, back: true },
  'round-host-into-stack': { moves: [{ at: HOST, dx: -430, dy: 120 }], steps: 30, back: true },
  'round-host-left': { moves: [{ at: HOST, dx: -900, dy: 0 }], steps: 40, back: true },
  'round-mon-up': { moves: [{ at: MON, dx: 0, dy: -420 }], steps: 40, back: true },
  // ...and the same trips with the hand taken off afterwards. Chips walk home
  // rather than teleport, so this is where they are supposed to have arrived.
  'round-host-above-settled': {
    moves: [{ at: HOST, dx: -300, dy: -420 }],
    steps: 30,
    back: true,
    passes: 90,
  },
  'round-host-into-stack-settled': {
    moves: [{ at: HOST, dx: -430, dy: 120 }],
    steps: 30,
    back: true,
    passes: 90,
  },
  'round-host-left-settled': {
    moves: [{ at: HOST, dx: -900, dy: 0 }],
    steps: 40,
    back: true,
    passes: 90,
  },
  'round-mon-up-settled': {
    moves: [{ at: MON, dx: 0, dy: -420 }],
    steps: 40,
    back: true,
    passes: 90,
  },
};

const only = process.argv.slice(2);
const names = only.length ? only : Object.keys(SCENARIOS);

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('pageerror', (e) => console.error('page error:', e.message));
await page.goto(URL, { waitUntil: 'networkidle' });

const summary = [];
for (const name of names) {
  const scene = await page.evaluate(PAGE, SCENARIOS[name]);
  const marks = score(scene);
  const worst = marks.detours.reduce((m, d) => Math.max(m, d.ratio), 0);
  const drift = marks.chips.reduce((m, c) => Math.max(m, c.drift), 0);
  const jump = marks.chips.reduce((m, c) => Math.max(m, c.jump), 0);
  const startles = marks.chips.reduce((n, c) => n + c.startles, 0);
  const still = scene.chipsWhenStill ?? { startles: 0, worst: 0 };
  const out = `${name}`;
  writeFileSync(
    `${out}.svg`,
    svg(scene, {
      title: name,
      crossings: marks.crossings.length,
      overlaps: marks.overlaps.length,
      inversions: marks.inversions.count,
      worst,
      drift,
      hits: marks.crossings,
      merged: marks.overlaps,
      land: marks.host,
    }),
  );
  execFileSync('magick', [`${out}.svg`, `${out}.png`]);
  summary.push({
    name,
    crossings: marks.crossings.length,
    overlaps: marks.overlaps.length,
    inversions: marks.inversions.count,
    worst,
    drift,
    jump,
    startles,
    still: `${still.startles}/${Math.round(still.worst)}px`,
  });

  console.log(`\n=== ${name}`);
  for (const hit of marks.crossings) {
    console.log(`  CROSS ${hit.a} × ${hit.b} at ${Math.round(hit.at.x)},${Math.round(hit.at.y)}`);
  }
  for (const run of marks.overlaps) {
    console.log(`  MERGED ${run.run}px (gap ${run.gap}) ${run.a} ‖ ${run.b}`);
  }
  for (const pair of marks.inversions.pairs) console.log(`  ORDER ${pair}`);
  for (const c of marks.chips) {
    console.log(
      `  CHIP ${c.text.padEnd(16)} drift=${String(c.drift).padStart(4)}px worstJump=${String(c.jump).padStart(4)}px  at ${Math.round(c.at.x)},${Math.round(c.at.y)}  home ${Math.round(c.home.x)},${Math.round(c.home.y)}`,
    );
  }
  console.log('host landings (top→bottom):');
  for (const l of marks.host) {
    console.log(`  ${l.side.padEnd(6)} ${Math.round(l.at.x)},${Math.round(l.at.y)}  ${l.wire}`);
  }
  if (process.env.DUMP) {
    for (const w of scene.wires) {
      console.log(
        `  PATH ${label(w).padEnd(44)} ${w.path.map((p) => `${Math.round(p.x)},${Math.round(p.y)}`).join(' → ')}`,
      );
    }
    for (const c of scene.cards) {
      console.log(`  CARD ${c.name.padEnd(20)} at ${Math.round(c.x)},${Math.round(c.y)} ±${c.hw},${c.hh}`);
    }
    for (const f of scene.frames) {
      console.log(
        `  FRAME ${f.name.padEnd(14)} x ${Math.round(f.x)}..${Math.round(f.x + f.width)}  y ${Math.round(f.y)}..${Math.round(f.y + f.height)}`,
      );
    }
    for (const c of scene.corridors) console.log(`  VIA ${c.leg.padEnd(60)} ${c.id}`);
    for (const f of scene.fans) console.log(`  FAN ${f.end.padEnd(60)} ${f.plan}`);
  }
  console.log('wires:');
  for (const d of marks.detours) {
    console.log(`  ${d.wire.padEnd(44)} len=${d.len} direct=${d.direct} ratio=${d.ratio} bends=${d.bends}`);
  }
}

console.log('\n--- summary');
for (const s of summary) {
  console.log(
    `${s.name.padEnd(22)} crossings=${s.crossings} merged=${s.overlaps} order=${s.inversions} worstRatio=${s.worst} chipDrift=${s.drift} chipJump=${s.jump} startles=${s.startles} whenStill=${s.still}`,
  );
}

await browser.close();
