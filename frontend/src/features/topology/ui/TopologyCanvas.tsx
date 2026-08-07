/**
 * Topology canvas — ELK homes, playful card drag, free group relocation, clickable edges.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GraphNode, Urn } from '../../../api/types';
import { TopologyLayout, type Point } from '../layout/elkLayout';
import { InteractiveLayout } from '../layout/interactiveLayout';
import { layoutLog, shortUrn } from '../layout/layoutDebug';
import type { TopologyGraph } from '../layout/prepareTopology';
import type { GraphState } from '../model/graphStore';
import { traceFrom } from '../model/graphStore';
import { draw, hitTest, hitTestEdge, hitTestGroup, type Scene, type Viewport } from './render';
import type { Palette } from './theme';

const MIN_ZOOM = 0.08;
const MAX_ZOOM = 6;
/** Fit packs the topology into the canvas without leaving a huge empty frame. */
const FIT_MAX_ZOOM = 2.2;
/**
 * How much of the free rectangle `fit` is allowed to claim.
 *
 * Must not exceed 1. The scale is the *smaller* of the two axis ratios, so
 * anything above 1 overflows whichever axis was binding — at 1.08 the demo
 * topology finished 42px underneath the legend and 3px underneath the topbar,
 * which is exactly the chrome the insets exist to avoid. A fit that has to be
 * panned afterwards is not a fit.
 */
const FIT_FILL = 1;
/** Ignore micro-moves so a click does not start physics or redefine a home. */
const DRAG_THRESHOLD_PX = 4;

interface Props {
  readonly graph: GraphState;
  /**
   * What to draw, decided by `prepareTopology`. Passed in rather than derived
   * here so the status bar and the canvas count the same cards.
   */
  readonly prepared: TopologyGraph;
  readonly palette: Palette;
  readonly selected: Urn | null;
  readonly selectedEdge: string | null;
  readonly traceDepth: number;
  readonly onSelect: (node: GraphNode | null) => void;
  readonly onSelectEdge: (key: string | null) => void;
  /**
   * Chrome that floats over the canvas and must not be fitted underneath —
   * the topbar, the legend, the inspector. The canvas is full-bleed on
   * purpose, so only `fit` knows these exist.
   */
  readonly inset?: { readonly top?: number; readonly right: number; readonly bottom: number };
  readonly fitToken?: number;
}

/**
 * What a press would select if it turns out to be a click.
 *
 * Selection is decided on press but only *applied* on release, and only when
 * the pointer never travelled: dragging a card or panning the view is a
 * gesture about position, not an inspection, so it must leave the inspector
 * showing whatever it was already showing.
 */
interface PendingClick {
  readonly node: GraphNode | null;
  readonly edge: string | null;
}

type DragTarget = {
  readonly pending: PendingClick;
  originX: number;
  originY: number;
  lastX: number;
  lastY: number;
  moved: boolean;
} & (
  | { kind: 'node'; urn: Urn; velocity: Point }
  | { kind: 'group'; urn: Urn }
  | { kind: 'pan' }
);

export function TopologyCanvas({
  graph,
  prepared,
  palette,
  selected,
  selectedEdge,
  traceDepth,
  onSelect,
  onSelectEdge,
  inset = { right: 0, bottom: 0 },
  fitToken = 0,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const elkRef = useRef(new TopologyLayout());
  const liveRef = useRef(new InteractiveLayout());
  const viewportRef = useRef<Viewport>({ x: 0, y: 0, zoom: 1 });
  const dragRef = useRef<DragTarget | null>(null);
  /** A reseed that arrived mid-drag and is waiting for the pointer to come up. */
  const seedPendingRef = useRef(false);
  const fittedRef = useRef(false);
  const insetRef = useRef(inset);
  insetRef.current = inset;
  const paletteRef = useRef(palette);
  paletteRef.current = palette;
  const [hoveredUrn, setHoveredUrn] = useState<Urn | null>(null);
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  const [hoverKind, setHoverKind] = useState<'node' | 'group' | 'edge' | null>(null);
  const [layoutEpoch, setLayoutEpoch] = useState(0);
  const [dragging, setDragging] = useState(false);
  const hoverRef = useRef<{ urn: Urn | null; edge: string | null; kind: typeof hoverKind }>({
    urn: null,
    edge: null,
    kind: null,
  });

  const setHoverAtomic = (urn: Urn | null, edge: string | null, kind: typeof hoverKind) => {
    const prev = hoverRef.current;
    if (prev.urn === urn && prev.edge === edge && prev.kind === kind) return;
    hoverRef.current = { urn, edge, kind };
    setHoveredUrn(urn);
    setHoveredEdge(edge);
    setHoverKind(kind);
  };

  const allNodes = useMemo(() => [...graph.nodes.values()], [graph.nodes]);
  const allEdges = useMemo(() => [...graph.edges.values()], [graph.edges]);

  const highlighted = useMemo(
    () => (selected ? traceFrom(graph, selected, traceDepth) : null),
    [graph, selected, traceDepth],
  );

  const sceneLive = useRef({
    allNodes,
    allEdges,
    selected,
    selectedEdge,
    highlighted,
    hovered: hoveredUrn,
    hoveredEdge,
  });
  sceneLive.current = {
    allNodes,
    allEdges,
    selected,
    selectedEdge,
    highlighted,
    hovered: hoveredUrn,
    hoveredEdge,
  };

  // Read at seed time rather than captured, so a seed deferred past a drag
  // uses the topology that is current when it finally lands.
  const preparedRef = useRef(prepared);
  preparedRef.current = prepared;
  const allNodesRef = useRef(allNodes);
  allNodesRef.current = allNodes;

  const applySeed = useCallback(() => {
    liveRef.current.seed(
      elkRef.current.positionsMap(),
      elkRef.current.groups(),
      elkRef.current.paths(),
      preparedRef.current.stackMemberUrns,
      allNodesRef.current,
    );
    setLayoutEpoch((n) => n + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void elkRef.current.sync(prepared).then((changed) => {
      if (cancelled || !changed) return;
      // `seed` cancels the drag it lands in and pulls every group back to its
      // ELK seat. Doing that under the pointer is the flicker: the card you
      // are holding lets go and the frame jumps out from under it. A container
      // starting somewhere else on the host is never a reason to do that, so
      // the reseed waits for the pointer to come up.
      if (dragRef.current) {
        seedPendingRef.current = true;
        return;
      }
      applySeed();
    });
    return () => {
      cancelled = true;
    };
  }, [prepared, applySeed]);

  const toWorld = useCallback((clientX: number, clientY: number): Point => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    const viewport = viewportRef.current;
    return {
      x: (clientX - rect.left - rect.width / 2 - viewport.x) / viewport.zoom,
      y: (clientY - rect.top - rect.height / 2 - viewport.y) / viewport.zoom,
    };
  }, []);

  const buildScene = (): Scene => {
    const live = sceneLive.current;
    const layout = liveRef.current;
    return {
      nodes: live.allNodes,
      edges: live.allEdges,
      positions: layout.positionsMap(),
      homes: layout.homesMap(),
      groupBounds: layout.groups(),
      selected: live.selected,
      selectedEdge: live.selectedEdge,
      highlighted: live.highlighted,
      hovered: live.hovered,
      hoveredEdge: live.hoveredEdge,
      dragging: layout.held,
    };
  };

  const sceneRef = useRef<Scene>(buildScene());

  /** Returns whether a viewport was actually computed — see the auto-fit latch. */
  const fit = useCallback((): boolean => {
    const canvas = canvasRef.current;
    const bounds = liveRef.current.bounds();
    if (!canvas || !bounds) return false;

    const rect = canvas.getBoundingClientRect();
    const { top = 0, right, bottom } = insetRef.current;
    const padding = 18;

    const usableWidth = Math.max(120, rect.width - right - padding * 2);
    const usableHeight = Math.max(120, rect.height - top - bottom - padding * 2);
    const graphWidth = Math.max(1, bounds.maxX - bounds.minX);
    const graphHeight = Math.max(1, bounds.maxY - bounds.minY);

    const zoom = Math.min(
      FIT_MAX_ZOOM,
      Math.max(MIN_ZOOM, Math.min(usableWidth / graphWidth, usableHeight / graphHeight) * FIT_FILL),
    );

    const centreX = (bounds.minX + bounds.maxX) / 2;
    const centreY = (bounds.minY + bounds.maxY) / 2;

    viewportRef.current = {
      zoom,
      // Centre inside the free rectangle, not inside the canvas: each inset
      // shifts the midpoint by half its size.
      x: -right / 2 - centreX * zoom,
      y: (top - bottom) / 2 - centreY * zoom,
    };
    return true;
  }, []);

  useEffect(() => {
    if (fitToken > 0) fit();
  }, [fitToken, fit]);

  /**
   * Fit once, on the first layout that has something in it.
   *
   * Re-fitting on every topology change moved the camera whenever a container
   * anywhere on the host started or stopped — the view you had arranged jumped
   * because something unrelated scaled the bounding box. After the first fit
   * the viewport belongs to the user; `Fit` is a button for a reason.
   *
   * The latch is spent by a fit that *happened*, not by one that was
   * attempted. The very first seed is the empty one that runs before the
   * snapshot lands, so `layoutEpoch` is already 1 by the time the graph
   * arrives; `fit` then found no bounds and returned without moving the
   * camera, and latching there left every real topology unfitted at zoom 1 —
   * a twenty-stack host opened showing about one card in fifty.
   */
  useEffect(() => {
    if (fittedRef.current || layoutEpoch === 0 || allNodes.length === 0) return;
    fittedRef.current = fit();
  }, [layoutEpoch, allNodes.length, fit]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let frame = 0;
    let running = true;
    // Measured on resize, not per frame: getBoundingClientRect inside the rAF
    // callback forces a synchronous layout every single frame, which is a
    // stutter the animation itself never causes.
    const surface = { width: 0, height: 0, dpr: 1 };

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      surface.width = rect.width;
      surface.height = rect.height;
      surface.dpr = dpr;
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
    };

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();

    // Dragging a window between displays changes devicePixelRatio without
    // changing the CSS box, so the ResizeObserver never fires and the canvas
    // stays at the old backing scale — a permanently soft picture.
    let dprWatch: MediaQueryList | null = null;
    const watchDpr = () => {
      dprWatch?.removeEventListener('change', onDprChange);
      dprWatch = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
      dprWatch.addEventListener('change', onDprChange);
    };
    function onDprChange() {
      resize();
      watchDpr();
    }
    watchDpr();

    const tick = (now: number) => {
      if (!running) return;
      if (liveRef.current.active) liveRef.current.step();
      sceneRef.current = buildScene();
      draw(ctx, sceneRef.current, viewportRef.current, paletteRef.current, surface, surface.dpr, now);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      running = false;
      cancelAnimationFrame(frame);
      observer.disconnect();
      dprWatch?.removeEventListener('change', onDprChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyClick = (pending: PendingClick) => {
    onSelect(pending.node);
    onSelectEdge(pending.edge);
  };

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    const world = toWorld(event.clientX, event.clientY);
    const grip = {
      lastX: event.clientX,
      lastY: event.clientY,
      originX: event.clientX,
      originY: event.clientY,
      moved: false,
    };
    const node = hitTest(sceneRef.current, world);
    if (node) {
      layoutLog('pointer:down', {
        target: 'node',
        urn: shortUrn(node.urn),
        kind: node.kind,
      });
      dragRef.current = {
        ...grip,
        kind: 'node',
        urn: node.urn,
        velocity: { x: 0, y: 0 },
        pending: { node, edge: null },
      };
    } else {
      const edge = hitTestEdge(sceneRef.current, world, viewportRef.current.zoom);
      if (edge) {
        layoutLog('pointer:down', { target: 'edge', key: edge.key, kind: edge.kind });
        dragRef.current = { ...grip, kind: 'pan', pending: { node: null, edge: edge.key } };
      } else {
        const groupUrn = hitTestGroup(sceneRef.current, world);
        if (groupUrn) {
          const stack = sceneRef.current.nodes.find((n) => n.urn === groupUrn) ?? null;
          layoutLog('pointer:down', { target: 'group-frame', urn: shortUrn(groupUrn) });
          dragRef.current = {
            ...grip,
            kind: 'group',
            urn: groupUrn,
            pending: { node: stack, edge: null },
          };
        } else {
          layoutLog('pointer:down', { target: 'pan' });
          dragRef.current = { ...grip, kind: 'pan', pending: { node: null, edge: null } };
        }
      }
    }
    setDragging(true);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;

    if (!drag) {
      const world = toWorld(event.clientX, event.clientY);
      const node = hitTest(sceneRef.current, world);
      if (node) {
        setHoverAtomic(node.urn, null, 'node');
      } else {
        const edge = hitTestEdge(sceneRef.current, world, viewportRef.current.zoom);
        if (edge) {
          setHoverAtomic(null, edge.key, 'edge');
        } else if (hitTestGroup(sceneRef.current, world)) {
          setHoverAtomic(null, null, 'group');
        } else {
          setHoverAtomic(null, null, null);
        }
      }
      return;
    }

    const zoom = viewportRef.current.zoom;

    // One threshold for every gesture: below it nothing has moved yet, so the
    // press is still a candidate click.
    const travel = Math.hypot(event.clientX - drag.originX, event.clientY - drag.originY);
    if (!drag.moved && travel < DRAG_THRESHOLD_PX) return;

    if (drag.kind === 'node') {
      const dx = event.clientX - drag.lastX;
      const dy = event.clientY - drag.lastY;
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      drag.moved = true;
      const world = toWorld(event.clientX, event.clientY);
      drag.velocity = {
        x: drag.velocity.x * 0.65 + (dx / zoom) * 0.35,
        y: drag.velocity.y * 0.65 + (dy / zoom) * 0.35,
      };
      liveRef.current.move(drag.urn, world);
    } else if (drag.kind === 'group') {
      const dx = event.clientX - drag.lastX;
      const dy = event.clientY - drag.lastY;
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      drag.moved = true;
      liveRef.current.translateGroup(drag.urn, dx / zoom, dy / zoom);
    } else {
      const dx = event.clientX - drag.lastX;
      const dy = event.clientY - drag.lastY;
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      drag.moved = true;
      viewportRef.current = {
        ...viewportRef.current,
        x: viewportRef.current.x + dx,
        y: viewportRef.current.y + dy,
      };
    }
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    event.currentTarget.releasePointerCapture(event.pointerId);
    const drag = dragRef.current;
    if (drag?.kind === 'node') {
      layoutLog('pointer:up', {
        target: 'node',
        urn: shortUrn(drag.urn),
        moved: drag.moved,
        action: drag.moved ? 'release+spring' : 'click-no-physics',
      });
      if (drag.moved) liveRef.current.release(drag.urn, drag.velocity);
    } else if (drag?.kind === 'group') {
      layoutLog('pointer:up', {
        target: 'group-frame',
        urn: shortUrn(drag.urn),
        moved: drag.moved,
        action: drag.moved ? 'releaseGroup→redefine-home' : 'cancelGroupDrag→keep-home',
      });
      if (drag.moved) liveRef.current.releaseGroup(drag.urn);
      else liveRef.current.cancelGroupDrag(drag.urn);
    } else if (drag?.kind === 'pan') {
      layoutLog('pointer:up', { target: 'pan', moved: drag.moved });
    }
    // A gesture that travelled was a drag or a pan; only a stationary press
    // inspects something.
    if (drag && !drag.moved) applyClick(drag.pending);
    dragRef.current = null;
    setDragging(false);

    if (seedPendingRef.current) {
      seedPendingRef.current = false;
      applySeed();
    }
  };

  const onWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const viewport = viewportRef.current;

    const factor = Math.exp(-event.deltaY * 0.0015);
    const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, viewport.zoom * factor));

    const px = event.clientX - rect.left - rect.width / 2;
    const py = event.clientY - rect.top - rect.height / 2;
    const scale = zoom / viewport.zoom;

    viewportRef.current = {
      zoom,
      x: px - (px - viewport.x) * scale,
      y: py - (py - viewport.y) * scale,
    };
  };

  return (
    <canvas
      ref={canvasRef}
      className="topology-canvas"
      style={{
        cursor: dragging
          ? 'grabbing'
          : hoverKind === 'edge'
            ? 'pointer'
            : hoverKind === 'node' || hoverKind === 'group'
              ? 'grab'
              : 'default',
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => {
        setHoverAtomic(null, null, null);
      }}
      onWheel={onWheel}
    />
  );
}
