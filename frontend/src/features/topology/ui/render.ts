/**
 * Canvas drawing — dock-style cards on a quiet canvas.
 *
 * The visual target: black surface, thin group frames with an inline
 * label, compact cards (title / subtitle / port chips) lifted by a soft
 * shadow, and hairline orthogonal links with flow dots on live dependencies.
 */

import type { GraphEdge, GraphNode, NodeKind, Urn } from '../../../api/types';
import type { Point } from '../layout/elkLayout';
import { isCanvasEdge } from '../layout/prepareTopology';
import { routeDrawnEdges, type RouteObstacle } from '../layout/liveEdges';
import { polylineLength, polylinePointAt } from '../layout/polyline';
import {
  EDGE_DASH,
  EDGE_REST_ALPHA,
  EDGE_REST_WEIGHT,
  NODE_SIZE,
  type Palette,
  cardIdentityColor,
  cardPorts,
  cardSubtitle,
  edgePortLabel,
  shapeOf,
  stackIdentityColor,
  statusOf,
} from './theme';
import { sizeOf } from '../layout/geometry';
import { placePortLabels, type Rect } from './portLabels';

export interface Viewport {
  readonly x: number;
  readonly y: number;
  readonly zoom: number;
}

export interface GroupBox {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface Scene {
  readonly nodes: readonly GraphNode[];
  readonly edges: readonly GraphEdge[];
  readonly positions: ReadonlyMap<Urn, Point>;
  /** Where every card belongs — the seat a dragged card springs back to. */
  readonly homes: ReadonlyMap<Urn, Point>;
  readonly groupBounds: ReadonlyMap<Urn, GroupBox>;
  readonly selected: Urn | null;
  readonly selectedEdge: string | null;
  readonly highlighted: ReadonlySet<Urn> | null;
  /**
   * Nodes the current operation scope would reach — Choose / All peers.
   * Drawn with a selection-style ring so they read as "also affected", not
   * merely "not dimmed".
   */
  readonly marked: ReadonlySet<Urn> | null;
  readonly hovered: Urn | null;
  readonly hoveredEdge: string | null;
  readonly dragging: Urn | null;
}

const LABEL_ZOOM = 0.2;
const DOT_GRID_ZOOM = 0.45;
const DOT_GRID_SPACING = 26;

const SANS = '"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif';
const MONO = '"IBM Plex Mono", ui-monospace, monospace';

const CARD_RADIUS = 10;
const FRAME_RADIUS = 16;
const PAD_X = 15;

/** Content block metrics — cards are laid out from the middle out. */
const TITLE_H = 15;
const SUBTITLE_H = 12;
const CHIP_H = 16;
const GAP_TITLE = 3;
const GAP_CHIP = 7;

/**
 * The edges that become wires on the canvas.
 *
 * Same rule as layout (`isCanvasEdge`): depends_on / mounts / exposed_on, plus
 * `hosts` only when it points at a watched unit or process — the link between
 * "the service I chose to watch" and "the host it runs on". A wholesale
 * `hosts` rail from every container is refused there and here for the same
 * reason.
 */
function drawnEdgesOf(scene: Scene): GraphEdge[] {
  const byUrn = new Map(scene.nodes.map((node) => [node.urn, node]));
  return scene.edges.filter((edge) => isCanvasEdge(edge, (urn) => byUrn.get(urn)?.kind));
}

/** Kind set for the router — whatever survived `drawnEdgesOf`. */
function drawnKindsOf(edges: readonly GraphEdge[]): ReadonlySet<string> {
  return new Set(edges.map((edge) => edge.kind));
}

/**
 * Port chip metrics — the `:18080` marker that sits on a published link.
 *
 * Always drawn on exposed_on wires so the topology shows reachability at a
 * glance. Stepping each chip further back along its own wire spreads fan-ins
 * on the host instead of stacking them in one column.
 */
const PORT_CHIP_H = 16;
const PORT_CHIP_PAD_X = 6;

/**
 * Flow-dot params — dots travel along live dependency links.
 *
 * Dots are placed by *distance* along the path and advanced by elapsed time, so
 * a link that grows or shrinks under a drag slides its dots instead of
 * rewriting where they are. Deriving positions from absolute time and a
 * length-dependent period, as this did, meant every change of length
 * repositioned every dot on that link in one frame.
 */
const DOT_SPEED = 160;
const DOT_SPACING = 150;
const MIN_DOTS = 3;
const MAX_DOTS = 8;
const DOT_RADIUS = 2.2;
const DOT_OPACITY = 0.9;
/** Longest step the flow may take in one frame — caps the catch-up after a
 * backgrounded tab, which otherwise resumes with the dots somewhere else. */
const MAX_FLOW_STEP_SEC = 1 / 20;

export function draw(
  ctx: CanvasRenderingContext2D,
  scene: Scene,
  viewport: Viewport,
  palette: Palette,
  size: { width: number; height: number },
  dpr: number,
  timeMs: number = performance.now(),
): void {
  ctx.save();
  ctx.scale(dpr, dpr);

  ctx.fillStyle = palette.surface;
  ctx.fillRect(0, 0, size.width, size.height);

  ctx.translate(size.width / 2 + viewport.x, size.height / 2 + viewport.y);
  ctx.scale(viewport.zoom, viewport.zoom);

  // Dots live in world space so panning reads as movement over a surface.
  drawDotGrid(ctx, size, viewport, palette);
  drawStackFrames(ctx, scene, viewport, palette);
  const portChips = drawEdges(ctx, scene, viewport, palette, timeMs);
  drawHomeGhost(ctx, scene, viewport, palette);
  drawNodes(ctx, scene, viewport, palette);
  // Ports last — cards used to paint over any chip that landed near them,
  // which is how a middle link "lost" its number entirely.
  drawLinkPorts(ctx, portChips, viewport, palette, collectOccupied(scene), [
    ...drawnRoutes.values(),
  ]);
  ctx.restore();
}

function drawDotGrid(
  ctx: CanvasRenderingContext2D,
  size: { width: number; height: number },
  viewport: Viewport,
  palette: Palette,
): void {
  if (viewport.zoom < DOT_GRID_ZOOM) return;

  const halfWidth = size.width / 2 / viewport.zoom;
  const halfHeight = size.height / 2 / viewport.zoom;
  const centreX = -viewport.x / viewport.zoom;
  const centreY = -viewport.y / viewport.zoom;

  const startX = Math.floor((centreX - halfWidth) / DOT_GRID_SPACING) * DOT_GRID_SPACING;
  const startY = Math.floor((centreY - halfHeight) / DOT_GRID_SPACING) * DOT_GRID_SPACING;
  const endX = centreX + halfWidth;
  const endY = centreY + halfHeight;
  const dot = Math.min(1.4 / viewport.zoom, 2);

  ctx.save();
  ctx.fillStyle = palette.grid;
  ctx.globalAlpha = 0.32;
  for (let x = startX; x <= endX; x += DOT_GRID_SPACING) {
    for (let y = startY; y <= endY; y += DOT_GRID_SPACING) {
      ctx.fillRect(x, y, dot, dot);
    }
  }
  ctx.restore();
}

function isDimmed(scene: Scene, urn: Urn): boolean {
  return scene.highlighted !== null && !scene.highlighted.has(urn);
}

function drawStackFrames(
  ctx: CanvasRenderingContext2D,
  scene: Scene,
  viewport: Viewport,
  palette: Palette,
): void {
  const byUrn = new Map(scene.nodes.map((n) => [n.urn, n]));

  for (const [stackUrn, box] of scene.groupBounds) {
    const stack = byUrn.get(stackUrn);
    if (!stack) continue;

    const accent = stackIdentityColor(stack.name);
    const selected = scene.selected === stackUrn;

    ctx.save();
    ctx.globalAlpha = isDimmed(scene, stackUrn) ? 0.25 : 1;

    roundedRectPath(ctx, box.x, box.y, box.width, box.height, FRAME_RADIUS);
    ctx.fillStyle = hexAlpha(accent, 0.1);
    ctx.fill();
    ctx.strokeStyle = selected ? palette.selection : hexAlpha(accent, 0.9);
    ctx.lineWidth = (selected ? 2.2 : 1.8) / viewport.zoom;
    ctx.shadowColor = accent;
    ctx.shadowBlur = (selected ? 14 : 8) / viewport.zoom;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Inline label: a dot and tracked caps sitting inside the frame padding.
    const labelX = box.x + 20;
    const labelY = box.y + 24;
    ctx.fillStyle = selected ? palette.selection : accent;
    ctx.beginPath();
    ctx.arc(labelX, labelY, 3.5, 0, Math.PI * 2);
    ctx.fill();

    ctx.font = `600 11px ${MONO}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    trackedText(ctx, stack.name.toUpperCase(), labelX + 12, labelY, 1.4);
    ctx.restore();
  }
}

function drawEdges(
  ctx: CanvasRenderingContext2D,
  scene: Scene,
  viewport: Viewport,
  palette: Palette,
  timeMs: number,
): {
  key: string;
  path: readonly Point[];
  text: string;
  color: string;
  alpha: number;
}[] {
  const baseWidth = 2.2 / viewport.zoom;
  const drawable = drawnEdgesOf(scene);
  const ordered = [...drawable].sort((a, b) => edgePriority(a.kind) - edgePriority(b.kind));
  const anchors = endpointAnchors(scene);
  const obstacles = collectObstacles(scene);
  const routed = routeDrawnEdges(drawable, anchors, drawnKindsOf(drawable), obstacles);
  drawnRoutes = routed;

  const flow: { key: string; path: Point[]; color: string; alpha: number }[] = [];
  const portChips: {
    key: string;
    path: readonly Point[];
    text: string;
    color: string;
    alpha: number;
  }[] = [];

  for (const edge of ordered) {
    const path = routed.get(edge.key);
    if (!path || path.length < 2) continue;

    const selected = scene.selectedEdge === edge.key;
    const hovered = scene.hoveredEdge === edge.key;
    const traced =
      scene.highlighted !== null &&
      scene.highlighted.has(edge.src) &&
      scene.highlighted.has(edge.dst);
    const dimmed =
      (isDimmed(scene, edge.src) || isDimmed(scene, edge.dst)) &&
      !selected &&
      scene.selectedEdge === null;
    const edgeFocus = scene.selectedEdge !== null && !selected;
    const isPrimary = edge.kind === 'depends_on' || edge.kind === 'mounts';

    // "Asked about" — the pointer is on this link, or on a card it joins, or a
    // trace runs through it. This is the only state that spends brightness.
    const asked =
      selected ||
      hovered ||
      traced ||
      scene.hovered === edge.src ||
      scene.hovered === edge.dst ||
      scene.selected === edge.src ||
      scene.selected === edge.dst;

    const color = palette.edgeColor[edge.kind] ?? palette.edge;
    const restAlpha = EDGE_REST_ALPHA[edge.kind] ?? 0.85;
    const restWeight = EDGE_REST_WEIGHT[edge.kind] ?? 1;
    const alpha = dimmed || edgeFocus ? 0.1 : asked ? 1 : restAlpha;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth =
      baseWidth * (selected ? 2.2 : hovered ? 1.7 : asked ? 1.35 : restWeight);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.setLineDash((EDGE_DASH[edge.kind] ?? []).map((s) => s / viewport.zoom));
    // Glow is a response, not a resting state — at rest it just thickens every
    // line by a few soft pixels and the canvas silts up.
    ctx.shadowColor = color;
    ctx.shadowBlur = (selected ? 14 : hovered ? 10 : 0) / viewport.zoom;

    strokeRoundedOrtho(ctx, path, 12 / viewport.zoom);

    if ((isPrimary || asked) && !dimmed) {
      ctx.shadowBlur = 0;
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      for (const end of [path[0], path[path.length - 1]]) {
        ctx.beginPath();
        ctx.arc(end.x, end.y, (selected ? 3.8 : hovered ? 3.2 : 2.4) / viewport.zoom, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.restore();

    if (edge.kind === 'depends_on' && !dimmed && !edgeFocus) {
      flow.push({ key: edge.key, path, color, alpha });
    }

    // Port numbers beside the wire, stepped back from the host so the host
    // card never covers them. Drawn after nodes (see `draw`) for the same reason.
    if (edge.kind === 'exposed_on' && !dimmed && !edgeFocus) {
      const text = edgePortLabel(edge);
      if (text) {
        portChips.push({
          key: edge.key,
          path,
          text,
          color,
          alpha: asked ? 1 : Math.max(alpha, 0.95),
        });
      }
    }
  }

  drawFlowDots(ctx, flow, viewport, timeMs);
  return portChips;
}

/**
 * Chips beside the published links.
 *
 * Measuring the text needs a context, so the widths are taken here and the
 * geometry is decided in `placePortLabels` — see that module for why a chip is
 * always anchored to its own wire rather than to a free patch of canvas.
 */
function drawLinkPorts(
  ctx: CanvasRenderingContext2D,
  chips: readonly {
    key: string;
    path: readonly Point[];
    text: string;
    color: string;
    alpha: number;
  }[],
  viewport: Viewport,
  palette: Palette,
  occupied: { cards: readonly Rect[]; frames: readonly Rect[] },
  wires: readonly (readonly Point[])[],
): void {
  if (chips.length === 0) return;

  ctx.save();
  ctx.font = `500 9.5px ${MONO}`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';

  const widths = chips.map((chip) => ctx.measureText(chip.text).width + PORT_CHIP_PAD_X * 2);
  const placements = placePortLabels(
    chips.map((chip, i) => ({ key: chip.key, path: chip.path, width: widths[i] })),
    occupied.cards,
    occupied.frames,
    wires,
    PORT_CHIP_H,
  );

  chips.forEach((chip, i) => {
    const width = widths[i];
    const { at } = placements[i];
    const x = at.x - width / 2;
    const y = at.y;

    roundedRectPath(ctx, x, y - PORT_CHIP_H / 2, width, PORT_CHIP_H, 4);
    ctx.globalAlpha = 1;
    ctx.fillStyle = palette.nodeFill;
    ctx.fill();
    ctx.globalAlpha = chip.alpha;
    ctx.strokeStyle = chip.color;
    ctx.lineWidth = 1.15 / viewport.zoom;
    ctx.stroke();

    ctx.fillStyle = palette.inkSecondary;
    ctx.fillText(chip.text, x + PORT_CHIP_PAD_X, y + 0.5);
  });

  ctx.restore();
}

/** Air around a chip so it does not graze a glowing frame stroke. */
const CHIP_CLEAR = 12;

/**
 * The two things a port chip has to keep off, kept apart.
 *
 * Cards are opaque and full of text; frames are a tinted outline with an empty
 * middle. `placePortLabels` prices them very differently — see `W_FRAME`.
 */
function collectOccupied(scene: Scene): { cards: Rect[]; frames: Rect[] } {
  const realized = realizedContainers(scene);
  const folded = new Set([...realized.values()].map((c) => c.urn));
  const cards: Rect[] = [];
  const frames: Rect[] = [];

  for (const node of scene.nodes) {
    if (node.kind === 'stack' || node.kind === 'image' || node.kind === 'network') continue;
    if (folded.has(node.urn)) continue;
    const pos = scene.positions.get(node.urn);
    if (!pos) continue;
    const [hw, hh] = sizeOf(node.kind);
    cards.push({
      left: pos.x - hw - CHIP_CLEAR,
      right: pos.x + hw + CHIP_CLEAR,
      top: pos.y - hh - CHIP_CLEAR,
      bottom: pos.y + hh + CHIP_CLEAR,
    });
  }

  for (const box of scene.groupBounds.values()) {
    frames.push({
      left: box.x - CHIP_CLEAR,
      right: box.x + box.width + CHIP_CLEAR,
      top: box.y - CHIP_CLEAR,
      bottom: box.y + box.height + CHIP_CLEAR,
    });
  }
  return { cards, frames };
}

/**
 * How far along its own link each flow has travelled, keyed by edge.
 *
 * Carrying the phase forward — rather than recomputing it from the clock — is
 * what keeps the dots continuous while the link underneath them moves: the
 * spacing may change, but the dots only ever advance from where they were.
 */
const flowPhase = new Map<string, number>();
const flowSeen = new Set<string>();
let flowClockMs = 0;

function drawFlowDots(
  ctx: CanvasRenderingContext2D,
  flow: readonly { key: string; path: Point[]; color: string; alpha: number }[],
  viewport: Viewport,
  timeMs: number,
): void {
  const elapsed =
    flowClockMs === 0 ? 0 : Math.min(MAX_FLOW_STEP_SEC, Math.max(0, (timeMs - flowClockMs) / 1000));
  flowClockMs = timeMs;

  if (flow.length === 0) {
    if (flowPhase.size > 0) flowPhase.clear();
    return;
  }

  const radius = DOT_RADIUS / viewport.zoom;
  const advance = elapsed * DOT_SPEED;
  flowSeen.clear();

  for (const anim of flow) {
    const length = polylineLength(anim.path);
    if (length < 1) continue;
    flowSeen.add(anim.key);

    // Spacing tracks length continuously, so the dot count stays inside its
    // bounds without any step where a dot pops into existence mid-link.
    const spacing = Math.min(Math.max(DOT_SPACING, length / MAX_DOTS), length / MIN_DOTS);
    const phase = ((flowPhase.get(anim.key) ?? 0) + advance) % spacing;
    flowPhase.set(anim.key, phase);

    ctx.save();
    ctx.fillStyle = anim.color;
    ctx.globalAlpha = anim.alpha * DOT_OPACITY;

    for (let travelled = phase; travelled <= length; travelled += spacing) {
      const pt = polylinePointAt(anim.path, travelled / length, length);
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, radius, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  for (const key of flowPhase.keys()) {
    if (!flowSeen.has(key)) flowPhase.delete(key);
  }
}

/**
 * Edge endpoints resolved once per frame — a folded container draws on its
 * service card, so its links have to land there too.
 */
function endpointAnchors(scene: Scene): Map<Urn, Point> {
  const anchors = new Map(scene.positions);
  for (const [service, container] of realizedContainers(scene)) {
    const seat = scene.positions.get(service);
    if (seat) anchors.set(container.urn, seat);
  }
  return anchors;
}

/** Cards (and folded containers) that links must route around. */
function collectObstacles(scene: Scene): RouteObstacle[] {
  const realized = realizedContainers(scene);
  const folded = new Set([...realized.values()].map((c) => c.urn));
  const out: RouteObstacle[] = [];

  for (const node of scene.nodes) {
    if (node.kind === 'stack' || node.kind === 'image' || node.kind === 'network') continue;
    if (folded.has(node.urn)) continue;
    const pos = scene.positions.get(node.urn);
    if (!pos) continue;
    const [hw, hh] = sizeOf(node.kind);
    out.push({ id: node.urn, x: pos.x, y: pos.y, hw, hh });
  }

  // Other stacks are solid: a host wire must not walk through a neighbouring
  // assembly. The source stack is ignored automatically — the endpoint sits
  // inside it (`covers` in liveEdges).
  for (const [stackUrn, box] of scene.groupBounds) {
    out.push({
      id: `frame:${stackUrn}`,
      x: box.x + box.width / 2,
      y: box.y + box.height / 2,
      hw: box.width / 2,
      hh: box.height / 2,
    });
  }
  return out;
}

/**
 * The seat a dragged card will spring back to — a dashed outline plus a
 * hairline tether, so play never feels like it might lose the arrangement.
 */
function drawHomeGhost(
  ctx: CanvasRenderingContext2D,
  scene: Scene,
  viewport: Viewport,
  palette: Palette,
): void {
  const urn = scene.dragging;
  if (!urn) return;
  const home = scene.homes.get(urn);
  const current = scene.positions.get(urn);
  if (!home || !current) return;

  const node = scene.nodes.find((n) => n.urn === urn);
  if (!node) return;
  const [halfWidth, halfHeight] = NODE_SIZE[shapeOf(node.kind)];
  if (Math.hypot(current.x - home.x, current.y - home.y) < 6) return;

  ctx.save();
  ctx.globalAlpha = 0.55;
  ctx.strokeStyle = palette.selection;
  ctx.lineWidth = 1.25 / viewport.zoom;
  ctx.setLineDash([6 / viewport.zoom, 6 / viewport.zoom]);

  ctx.beginPath();
  ctx.moveTo(current.x, current.y);
  ctx.lineTo(home.x, home.y);
  ctx.globalAlpha = 0.25;
  ctx.stroke();

  ctx.globalAlpha = 0.5;
  roundedRectPath(ctx, home.x - halfWidth, home.y - halfHeight, halfWidth * 2, halfHeight * 2, CARD_RADIUS);
  ctx.stroke();
  ctx.restore();
}

/** Stroke an orthogonal polyline with rounded corners. */
function strokeRoundedOrtho(
  ctx: CanvasRenderingContext2D,
  points: readonly Point[],
  radius: number,
): void {
  if (points.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);

  for (let i = 1; i < points.length - 1; i += 1) {
    const prev = points[i - 1];
    const curr = points[i];
    const next = points[i + 1];
    const toPrev = Math.hypot(curr.x - prev.x, curr.y - prev.y);
    const toNext = Math.hypot(next.x - curr.x, next.y - curr.y);
    const r = Math.min(radius, toPrev / 2, toNext / 2);
    if (r < 0.5) {
      ctx.lineTo(curr.x, curr.y);
      continue;
    }
    const ax = curr.x - ((curr.x - prev.x) / (toPrev || 1)) * r;
    const ay = curr.y - ((curr.y - prev.y) / (toPrev || 1)) * r;
    const bx = curr.x + ((next.x - curr.x) / (toNext || 1)) * r;
    const by = curr.y + ((next.y - curr.y) / (toNext || 1)) * r;
    ctx.lineTo(ax, ay);
    ctx.quadraticCurveTo(curr.x, curr.y, bx, by);
  }

  const last = points[points.length - 1];
  ctx.lineTo(last.x, last.y);
  ctx.stroke();
}

/** Draw order — later wins the overlap. Long host links go underneath. */
function edgePriority(kind: string): number {
  if (kind === 'hosts') return -2;
  if (kind === 'exposed_on') return -1;
  if (kind === 'mounts') return 0;
  if (kind === 'depends_on') return 1;
  return 0;
}

function drawNodes(
  ctx: CanvasRenderingContext2D,
  scene: Scene,
  viewport: Viewport,
  palette: Palette,
): void {
  const showDetail = viewport.zoom >= LABEL_ZOOM;
  const realized = realizedContainers(scene);
  const containerOfService = new Map<Urn, GraphNode>();
  for (const [serviceUrn, container] of realized) {
    containerOfService.set(serviceUrn, container);
  }
  const folded = new Set([...realized.values()].map((c) => c.urn));

  for (const node of scene.nodes) {
    if (node.kind === 'stack' || node.kind === 'image' || node.kind === 'network') continue;
    if (folded.has(node.urn)) continue;
    if (!scene.positions.has(node.urn)) continue;

    const position = scene.positions.get(node.urn)!;
    const [halfWidth, halfHeight] = NODE_SIZE[shapeOf(node.kind)];
    const selected = scene.selected === node.urn;
    const marked = !selected && (scene.marked?.has(node.urn) ?? false);
    const hovered = scene.hovered === node.urn;
    const dragged = scene.dragging === node.urn;
    const dimmed = isDimmed(scene, node.urn);
    const display = enrichCard(node, containerOfService.get(node.urn));
    const status = statusOf(display.statusNode);
    const identity = cardIdentityColor(node);
    const accent =
      identity ?? (status !== 'neutral' ? palette.status[status] : kindAccent(node.kind, palette));

    ctx.save();
    ctx.globalAlpha = dimmed ? 0.22 : 1;
    ctx.translate(position.x, position.y);
    // A dragged card lifts off the surface — the cue that this is play.
    if (dragged) ctx.scale(1.04, 1.04);

    // Card body on a soft shadow — light identity wash for containers.
    ctx.save();
    ctx.shadowColor = palette.shadow;
    ctx.shadowBlur = dragged ? 26 : 14;
    ctx.shadowOffsetY = dragged ? 10 : 4;
    roundedRectPath(ctx, -halfWidth, -halfHeight, halfWidth * 2, halfHeight * 2, CARD_RADIUS);
    ctx.fillStyle = identity ? mixHex(palette.nodeFill, identity, 0.14) : palette.nodeFill;
    ctx.fill();
    ctx.restore();

    ctx.strokeStyle = selected || marked
      ? palette.selection
      : hovered || dragged
        ? identity ?? palette.inkMuted
        : identity
          ? hexAlpha(identity, 0.55)
          : palette.nodeStroke;
    ctx.lineWidth = (selected || marked ? 1.8 : identity ? 1.45 : 1.15) / viewport.zoom;
    roundedRectPath(ctx, -halfWidth, -halfHeight, halfWidth * 2, halfHeight * 2, CARD_RADIUS);
    ctx.stroke();

    if (selected || marked || hovered || dragged) {
      roundedRectPath(
        ctx,
        -halfWidth - 5,
        -halfHeight - 5,
        halfWidth * 2 + 10,
        halfHeight * 2 + 10,
        CARD_RADIUS + 4,
      );
      ctx.strokeStyle = palette.selection;
      ctx.globalAlpha = (dimmed ? 0.22 : 1) * (selected ? 0.55 : marked ? 0.4 : 0.28);
      ctx.lineWidth = 1.4 / viewport.zoom;
      ctx.stroke();
      ctx.globalAlpha = dimmed ? 0.22 : 1;
    }

    // Identity rail — unique per container; status stays on the LED.
    if (node.kind !== 'volume') {
      const inset = 9;
      roundedRectPath(ctx, -halfWidth + 5, -halfHeight + inset, 3.5, halfHeight * 2 - inset * 2, 1.5);
      ctx.fillStyle = accent;
      ctx.fill();
    }

    // Status LED with a soft halo.
    if (status !== 'neutral') {
      const lx = halfWidth - 15;
      const ly = -halfHeight + 15;
      ctx.beginPath();
      ctx.arc(lx, ly, 6, 0, Math.PI * 2);
      ctx.fillStyle = hexAlpha(palette.status[status], 0.18);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(lx, ly, 3.6, 0, Math.PI * 2);
      ctx.fillStyle = palette.status[status];
      ctx.fill();
    }

    if (showDetail) {
      drawCardContent(ctx, display, node.kind, halfWidth, palette, accent);
    }

    ctx.restore();
  }
}

function drawCardContent(
  ctx: CanvasRenderingContext2D,
  display: CardDisplay,
  kind: NodeKind,
  halfWidth: number,
  palette: Palette,
  accent: string,
): void {
  const isVolume = kind === 'volume';
  const textLeft = -halfWidth + PAD_X + (isVolume ? 28 : 0);
  const maxWidth = halfWidth * 2 - PAD_X * 2 - (isVolume ? 28 : 0) - 14;

  const hasChip = display.ports !== null;
  const blockHeight =
    TITLE_H + GAP_TITLE + SUBTITLE_H + (hasChip ? GAP_CHIP + CHIP_H : 0);
  let cursor = -blockHeight / 2;

  const titleY = cursor + TITLE_H / 2;
  cursor += TITLE_H + GAP_TITLE;
  const subtitleY = cursor + SUBTITLE_H / 2;
  cursor += SUBTITLE_H + GAP_CHIP;
  const chipY = cursor + CHIP_H / 2;

  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';

  if (isVolume) {
    drawVolumeIcon(ctx, -halfWidth + PAD_X + 9, 0, accent);
  }

  ctx.fillStyle = palette.ink;
  ctx.font = `600 13px ${SANS}`;
  ctx.fillText(ellipsize(ctx, display.title, maxWidth), textLeft, titleY);

  ctx.fillStyle = palette.inkMuted;
  ctx.font = `400 10.5px ${MONO}`;
  ctx.fillText(ellipsize(ctx, display.subtitle, maxWidth), textLeft, subtitleY);

  if (display.ports) {
    drawPortChips(ctx, display.ports, textLeft, chipY, palette);
  }
}

/** Cylinder mark — volumes read as storage, not as another service. */
function drawVolumeIcon(
  ctx: CanvasRenderingContext2D,
  cx: number,
  cy: number,
  color: string,
): void {
  const rx = 8;
  const ry = 3.2;
  const height = 13;
  const top = cy - height / 2;
  const bottom = cy + height / 2;

  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.3;
  ctx.globalAlpha = 0.9;

  ctx.beginPath();
  ctx.ellipse(cx, top, rx, ry, 0, 0, Math.PI * 2);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(cx - rx, top);
  ctx.lineTo(cx - rx, bottom);
  ctx.moveTo(cx + rx, top);
  ctx.lineTo(cx + rx, bottom);
  ctx.stroke();

  ctx.beginPath();
  ctx.ellipse(cx, bottom, rx, ry, 0, 0, Math.PI);
  ctx.stroke();
  ctx.restore();
}

function drawPortChips(
  ctx: CanvasRenderingContext2D,
  portsLine: string,
  x: number,
  y: number,
  palette: Palette,
): void {
  const chips = portsLine.split('  ').filter(Boolean).slice(0, 2);
  let cursor = x;
  ctx.font = `400 9.5px ${MONO}`;
  for (const chip of chips) {
    const width = ctx.measureText(chip).width;
    const padX = 6;
    roundedRectPath(ctx, cursor, y - CHIP_H / 2, width + padX * 2, CHIP_H, 5);
    ctx.fillStyle = palette.surface;
    ctx.fill();
    ctx.strokeStyle = palette.nodeStroke;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = palette.inkSecondary;
    ctx.fillText(chip, cursor + padX, y + 0.5);
    cursor += width + padX * 2 + 5;
  }
}

/**
 * Folding depends only on the graph, not on where anything currently is, but
 * it was being rebuilt from scratch several times per frame — by the anchors,
 * the obstacles, the cards, and again by every hit test. The graph arrays are
 * referentially stable between snapshots, so identity is a sound cache key.
 */
let realizedNodes: readonly GraphNode[] | null = null;
let realizedEdges: readonly GraphEdge[] | null = null;
let realizedCache = new Map<Urn, GraphNode>();

function realizedContainers(scene: Scene): Map<Urn, GraphNode> {
  if (scene.nodes === realizedNodes && scene.edges === realizedEdges) return realizedCache;

  const byUrn = new Map(scene.nodes.map((n) => [n.urn, n]));
  const map = new Map<Urn, GraphNode>();
  for (const edge of scene.edges) {
    if (edge.kind !== 'realized_by') continue;
    const container = byUrn.get(edge.dst);
    if (container?.kind === 'container') map.set(edge.src, container);
  }

  realizedNodes = scene.nodes;
  realizedEdges = scene.edges;
  realizedCache = map;
  return map;
}

interface CardDisplay {
  title: string;
  subtitle: string;
  ports: string | null;
  statusNode: GraphNode;
}

function enrichCard(node: GraphNode, container: GraphNode | undefined): CardDisplay {
  if (node.kind === 'service' && container) {
    return {
      title: node.name,
      // The kind word is the service's; the image is the container's. Both are
      // needed, and neither is the other's: this card is a compose service,
      // and what it runs is a fact about the container folded into it.
      subtitle: cardSubtitle(node, container),
      ports: cardPorts(container),
      // The state does not. The Controller folds every replica's state into
      // the service (`mapper.py`), so this is the whole service rather than
      // whichever container `realizedContainers` happened to keep last — and
      // it is the same node the inspector reads, which is what stops a green
      // dot from sitting beside a panel that disagrees with it. Falls back to
      // the container for a Controller old enough not to send one.
      statusNode: node.status ? node : container,
    };
  }
  if (node.kind === 'volume') {
    return {
      title: friendlyVolumeName(node),
      subtitle: cardSubtitle(node),
      ports: null,
      statusNode: node,
    };
  }
  return {
    title: node.name,
    subtitle: cardSubtitle(node),
    ports: cardPorts(node),
    statusNode: node,
  };
}

/**
 * Prefer the compose volume's declared name — `data_pg_data` is the docker
 * name, `pg_data` is what the author wrote. Splitting on `_` would also eat
 * the underscore inside the name itself.
 */
function friendlyVolumeName(node: GraphNode): string {
  const declared = node.labels['com.docker.compose.volume'];
  if (declared) return declared;

  const name = node.name;
  if (/^[a-f0-9]{12,}$/i.test(name)) return `vol ${name.slice(0, 8)}`;

  const project = node.labels['com.docker.compose.project'];
  if (project && name.startsWith(`${project}_`)) return name.slice(project.length + 1);
  return name;
}

function kindAccent(kind: NodeKind, palette: Palette): string {
  switch (kind) {
    case 'volume':
      return palette.kindAccent.volume;
    case 'network':
      return palette.kindAccent.network;
    default:
      return palette.kindAccent.default;
  }
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}

/** Letter-spaced caps — canvas has no tracking, so step glyph by glyph. */
function trackedText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  tracking: number,
): void {
  let cursor = x;
  for (const char of text) {
    ctx.fillText(char, cursor, y);
    cursor += ctx.measureText(char).width + tracking;
  }
}

function hexAlpha(hex: string, alpha: number): string {
  const raw = hex.replace('#', '');
  const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw;
  const n = Number.parseInt(full, 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Blend `amount` of accent into base (0–1). */
function mixHex(base: string, accent: string, amount: number): string {
  const parse = (hex: string) => {
    const raw = hex.replace('#', '');
    const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw;
    const n = Number.parseInt(full, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255] as const;
  };
  const [br, bg, bb] = parse(base);
  const [ar, ag, ab] = parse(accent);
  const t = Math.max(0, Math.min(1, amount));
  const r = Math.round(br + (ar - br) * t);
  const g = Math.round(bg + (ag - bg) * t);
  const b = Math.round(bb + (ab - bb) * t);
  return `rgb(${r},${g},${b})`;
}

/** Trim to the measured width of the card, not to a guessed character count. */
function ellipsize(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (maxWidth <= 0 || ctx.measureText(text).width <= maxWidth) return text;
  let cut = text.length - 1;
  while (cut > 1 && ctx.measureText(`${text.slice(0, cut)}…`).width > maxWidth) cut -= 1;
  return `${text.slice(0, cut)}…`;
}

export function hitTest(scene: Scene, world: Point): GraphNode | null {
  const realized = realizedContainers(scene);
  const hiddenContainers = new Set([...realized.values()].map((c) => c.urn));

  for (let i = scene.nodes.length - 1; i >= 0; i -= 1) {
    const node = scene.nodes[i];
    if (node.kind === 'stack' || node.kind === 'image' || node.kind === 'network') continue;
    if (hiddenContainers.has(node.urn)) continue;
    const position = scene.positions.get(node.urn);
    if (!position) continue;
    const [halfWidth, halfHeight] = NODE_SIZE[shapeOf(node.kind)];
    if (
      Math.abs(world.x - position.x) <= halfWidth &&
      Math.abs(world.y - position.y) <= halfHeight
    ) {
      return node;
    }
  }
  return null;
}

/** Hit the empty area of a stack frame (not a child card). */
export function hitTestGroup(scene: Scene, world: Point): Urn | null {
  if (hitTest(scene, world)) return null;

  let best: { urn: Urn; area: number } | null = null;
  for (const [urn, box] of scene.groupBounds) {
    if (
      world.x < box.x ||
      world.y < box.y ||
      world.x > box.x + box.width ||
      world.y > box.y + box.height
    ) {
      continue;
    }
    const area = box.width * box.height;
    if (!best || area < best.area) best = { urn, area };
  }
  return best?.urn ?? null;
}

/** Default edge hit slop in screen pixels. Touch/narrow chrome passes a wider value. */
export const EDGE_HIT_PX = 16;
export const EDGE_HIT_PX_TOUCH = 24;

/**
 * The routes the last frame actually drew.
 *
 * Hit-testing against these instead of solving the whole graph again is what
 * keeps a pointer move cheap — and it tests the wires the user can see, which
 * a fresh solve does not guarantee.
 */
let drawnRoutes: ReadonlyMap<string, Point[]> = new Map();

/** Nearest drawn edge within a hit threshold (world units). */
export function hitTestEdge(
  scene: Scene,
  world: Point,
  zoom: number,
  hitPx: number = EDGE_HIT_PX,
): GraphEdge | null {
  if (hitTest(scene, world)) return null;

  const drawable = drawnEdgesOf(scene);
  const paths = drawnRoutes.size
    ? drawnRoutes
    : routeDrawnEdges(
        drawable,
        endpointAnchors(scene),
        drawnKindsOf(drawable),
        collectObstacles(scene),
      );
  const threshold = hitPx / zoom;
  let best: { edge: GraphEdge; dist: number } | null = null;

  for (const edge of drawable) {
    const path = paths.get(edge.key);
    if (!path || path.length < 2) continue;
    const dist = distanceToPolyline(world, path);
    if (dist > threshold) continue;
    if (!best || dist < best.dist) best = { edge, dist };
  }
  return best?.edge ?? null;
}

function distanceToPolyline(point: Point, path: readonly Point[]): number {
  let best = Infinity;
  for (let i = 1; i < path.length; i += 1) {
    best = Math.min(best, distanceToSegment(point, path[i - 1], path[i]));
  }
  return best;
}

function distanceToSegment(p: Point, a: Point, b: Point): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lenSq = dx * dx + dy * dy;
  if (lenSq < 1e-6) return Math.hypot(p.x - a.x, p.y - a.y);
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}
