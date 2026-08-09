/**
 * Application shell — canvas-first, DockGraph-style minimal chrome.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CommandKind, GraphNode, Urn } from './api/types';
import { prepareTopologyGraph } from './features/topology/layout/prepareTopology';
import { neighborsOf } from './features/topology/model/graphStore';
import { deriveStatus, explainEmpty } from './features/topology/model/status';
import { pendingCount } from './features/hosts/model/hosts';
import { useFleet } from './features/hosts/model/useFleet';
import { HostsPanel } from './features/hosts/ui/HostsPanel';
import { useActivity } from './features/activity/model/useActivity';
import { ActivityPanel } from './features/activity/ui/ActivityPanel';
import { useLogs } from './features/logs/model/useLogs';
import { LogsPanel } from './features/logs/ui/LogsPanel';
import { useOperations } from './features/operations/model/useOperations';
import { ActionBar } from './features/operations/ui/ActionBar';
import { useGraphStream } from './features/topology/model/useGraphStream';
import { useHealth } from './features/topology/model/useHealth';
import { EmptyState, StatusBanner } from './features/topology/ui/CanvasOverlay';
import { Legend } from './features/topology/ui/Legend';
import { NodeInspector } from './features/topology/ui/NodeInspector';
import { TopologyCanvas } from './features/topology/ui/TopologyCanvas';
import { EDGE_HIT_PX, EDGE_HIT_PX_TOUCH } from './features/topology/ui/render';
import { DARK, LIGHT } from './features/topology/ui/theme';
import { ZOOM_BUTTON_FACTOR } from './features/topology/ui/viewport';
import { useMediaQuery } from './lib/useMediaQuery';

const API_BASE = import.meta.env.VITE_API_BASE ?? window.location.origin;

/** Keep in step with the `@media (max-width: 720px)` chrome in `index.css`. */
const NARROW_QUERY = '(max-width: 720px)';
const COARSE_POINTER_QUERY = '(pointer: coarse)';

const INSPECTOR_WIDTH = 320;
/** The hosts panel, on the other side. Keep in step with `.hosts` in CSS. */
const HOSTS_WIDTH = 340;
/**
 * The legend plus the status bar under it, measured from the canvas bottom.
 * Keep in step with `.legend` in CSS — it is 107px tall and sits 42px up, and
 * the old 120 here let `fit` finish 11px underneath its first line.
 */
const LEGEND_HEIGHT = 150;
const LEGEND_HEIGHT_NARROW = 118;
/** The topbar floats over a full-bleed canvas, so `fit` has to allow for it. */
const TOPBAR_HEIGHT = 58;
/** Trace row + wrapped actions; keep in step with narrow `.topbar--slim`. */
const TOPBAR_HEIGHT_NARROW = 132;
/** So does the banner, when there is one. Keep in step with `.banner` in CSS. */
const BANNER_HEIGHT = 33;
/** Docker's own networks. Present on every host, so counting them says nothing. */
const DEFAULT_NETWORKS = new Set(['bridge', 'host', 'none']);

export default function App() {
  const [dark, setDark] = useState(true);
  const [selected, setSelected] = useState<Urn | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [traceDepth, setTraceDepth] = useState(1);
  const [query, setQuery] = useState('');
  const [fitToken, setFitToken] = useState(0);
  const [zoomToken, setZoomToken] = useState(0);
  const [zoomFactor, setZoomFactor] = useState(1);
  const [hostsOpen, setHostsOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const narrow = useMediaQuery(NARROW_QUERY);
  const coarsePointer = useMediaQuery(COARSE_POINTER_QUERY);

  const { graph, connection, resyncs } = useGraphStream(API_BASE);
  const health = useHealth(API_BASE);
  // Polled faster while the panel is open: a host that has just been given the
  // install command is the thing the operator is watching for.
  const fleet = useFleet(API_BASE, hostsOpen);
  const activity = useActivity(API_BASE, activityOpen);
  const palette = dark ? DARK : LIGHT;

  const status = useMemo(() => deriveStatus(connection, health), [connection, health]);

  // One decision about what the canvas shows, shared with the canvas itself so
  // the counters below cannot describe a picture that was never drawn.
  const prepared = useMemo(
    () => prepareTopologyGraph([...graph.nodes.values()], [...graph.edges.values()]),
    [graph.nodes, graph.edges],
  );

  const emptyExplanation =
    prepared.placed.size === 0 ? explainEmpty(status, health) : null;

  const selectedNode = selected ? (graph.nodes.get(selected) ?? null) : null;
  const selectedLink = selectedEdge ? (graph.edges.get(selectedEdge) ?? null) : null;
  const selectedEdges = useMemo(
    () => (selected ? neighborsOf(graph, selected) : []),
    [graph, selected],
  );

  const resolveName = useCallback(
    (urn: Urn) => graph.nodes.get(urn)?.name ?? urn,
    [graph.nodes],
  );

  /**
   * An engine id is also a host URN — that is ADR-0011's whole point, and it
   * is what lets the hosts panel show `lab-node-01` instead of twelve hex
   * characters. Null until discovery has produced the node, which for a
   * pending host it never has.
   */
  const resolveHostName = useCallback(
    (engineId: string) => graph.nodes.get(`bystack:host:${engineId}`)?.name ?? null,
    [graph.nodes],
  );

  // Operations are scoped to whatever is selected. Only nodes the Controller
  // will act on get an action bar, and it decides which — not this component.
  const operations = useOperations(API_BASE, selected, graph.nodes);

  // Reads nothing until the panel is opened. See `useLogs` — a fetch per
  // selection would put an agent round trip behind clicking through a canvas.
  const logs = useLogs(API_BASE, selected);

  /**
   * Run, then refresh the timeline at once.
   *
   * Operations are not on the delta stream — deliberately, since a command
   * does not change the graph and discovery does (ARCHITECTURE §9) — so the
   * one moment the timeline exists for produces no event to subscribe to.
   * Waiting out the poll interval to see your own action appear reads as a
   * click that missed.
   */
  const runOperation = useCallback(
    async (kind: CommandKind) => {
      await operations.run(kind);
      activity.refresh();
    },
    [operations, activity],
  );

  /**
   * Two different questions, kept visibly apart.
   *
   * The headline numbers count cards actually on the canvas. The muted line
   * counts what was discovered — including networks, which are deliberately
   * drawn as stack frames rather than as cards. A single figure covering both
   * is the bug this replaces: it reported networks nothing could point at.
   */
  const counts = useMemo(() => {
    let workloads = 0;
    let volumes = 0;
    const drawn = [...prepared.freeNodes, ...[...prepared.stackMembers.values()].flat()];
    for (const node of drawn) {
      if (node.kind === 'service' || node.kind === 'container') workloads += 1;
      else if (node.kind === 'volume') volumes += 1;
    }

    let containers = 0;
    let networks = 0;
    for (const node of graph.nodes.values()) {
      if (node.kind === 'container') containers += 1;
      else if (node.kind === 'network' && !DEFAULT_NETWORKS.has(node.name)) networks += 1;
    }

    return { workloads, volumes, stacks: prepared.stacks.length, containers, networks };
  }, [graph.nodes, prepared]);

  const matches = useMemo(() => {
    if (!query.trim()) return [];
    const needle = query.trim().toLowerCase();
    return [...graph.nodes.values()]
      .filter((node) => node.kind !== 'stack' && node.name.toLowerCase().includes(needle))
      .slice(0, 8);
  }, [graph.nodes, query]);

  // `/healthz` knows the sources even when none of them has produced a node
  // yet; the graph only knows them once discovery has succeeded.
  const providers = health.kind === 'reached' ? health.health.providers : [];
  // `null` until the first health answer, which reads the same as an agent
  // whose version we do not know: unknown, not behind (`versionSkew`).
  const controllerVersion = health.kind === 'reached' ? health.health.version : null;
  const providerLabel =
    providers.length > 1
      ? `${providers.length} sources`
      : (providers[0]?.id ??
        [...graph.nodes.values()].find((node) => node.kind === 'host')?.source ??
        'local');
  const readOnly = health.kind === 'reached' && health.health.read_only;
  const pending = pendingCount(fleet.agents);

  const inspectorOpen = selectedNode !== null || selectedLink !== null;
  const bannerVisible = status.banner && status.detail !== null;
  const sheetOpen = narrow && (hostsOpen || activityOpen || inspectorOpen);

  const clearSelection = useCallback(() => {
    setSelected(null);
    setSelectedEdge(null);
  }, []);

  const closeSheets = useCallback(() => {
    setHostsOpen(false);
    setActivityOpen(false);
    clearSelection();
  }, [clearSelection]);

  const bumpZoom = useCallback((factor: number) => {
    setZoomFactor(factor);
    setZoomToken((n) => n + 1);
  }, []);

  // Escape dismisses floating chrome the same way the sheet backdrop does.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (!hostsOpen && !activityOpen && !inspectorOpen) return;
      event.preventDefault();
      closeSheets();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [hostsOpen, activityOpen, inspectorOpen, closeSheets]);

  // The two left-hand panels answer different questions and share one edge of
  // the screen, so opening either closes the other. Stacking them would put
  // the canvas — the thing both of them are about — in a strip.
  const openHosts = useCallback(() => {
    setHostsOpen((open) => {
      const next = !open;
      if (next) {
        setActivityOpen(false);
        if (narrow) clearSelection();
      }
      return next;
    });
  }, [narrow, clearSelection]);

  const openActivity = useCallback(() => {
    setActivityOpen((open) => {
      const next = !open;
      if (next) {
        setHostsOpen(false);
        if (narrow) clearSelection();
      }
      return next;
    });
  }, [narrow, clearSelection]);

  const topbarPad = narrow ? TOPBAR_HEIGHT_NARROW : TOPBAR_HEIGHT;
  const legendPad = narrow ? LEGEND_HEIGHT_NARROW : LEGEND_HEIGHT;

  return (
    <div
      className={`app${narrow ? ' app--narrow' : ''}`}
      data-theme={dark ? 'dark' : 'light'}
    >
      <header className="topbar topbar--slim">
        <div className="brand brand--mark">ByStack</div>

        <div className="status-pill" title={status.detail ?? undefined}>
          <span className={`live-dot live-dot--${status.tone}`} />
          <span className="status-pill__text">
            {narrow ? status.label : (
              <>
                {providerLabel}
                <span className="brand__sep">·</span>
                {status.label}
              </>
            )}
            {!narrow && readOnly ? (
              <>
                <span className="brand__sep">·</span>
                read-only
              </>
            ) : null}
            {resyncs > 0 ? (
              <>
                <span className="brand__sep">·</span>
                {resyncs} resync
              </>
            ) : null}
          </span>
        </div>

        <div className="topbar__search">
          <input
            className="search"
            type="search"
            placeholder="Search…"
            enterKeyHint="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />

          {matches.length > 0 && (
            <ul className="search__results">
              {matches.map((node) => (
                <li key={node.urn}>
                  <button
                    onClick={() => {
                      setSelected(node.urn);
                      setSelectedEdge(null);
                      setQuery('');
                      if (narrow) setHostsOpen(false);
                    }}
                  >
                    {node.name} <span className="muted">{node.kind}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <label className="control control--trace">
          Trace
          <input
            type="range"
            min={1}
            max={4}
            value={traceDepth}
            onChange={(event) => setTraceDepth(Number(event.target.value))}
            aria-label="Trace depth"
          />
          <span>{traceDepth}</span>
        </label>

        <div className="spacer" />

        <div className="topbar__actions">
          {/* The count rides on the closed button on purpose: a host that has
              just enrolled is waiting on a person, and it should not need the
              panel to be open to say so. */}
          <button
            className={`control${pending > 0 ? ' control--attention' : ''}`}
            onClick={openHosts}
          >
            Hosts
            {pending > 0 ? <span className="control__badge">{pending}</span> : null}
          </button>
          <button className="control" onClick={openActivity}>
            Activity
          </button>
          {narrow ? (
            <>
              <button
                type="button"
                className="control control--zoom"
                aria-label="Zoom out"
                onClick={() => bumpZoom(1 / ZOOM_BUTTON_FACTOR)}
              >
                −
              </button>
              <button
                type="button"
                className="control control--zoom"
                aria-label="Zoom in"
                onClick={() => bumpZoom(ZOOM_BUTTON_FACTOR)}
              >
                +
              </button>
            </>
          ) : null}
          <button className="control" onClick={() => setFitToken((n) => n + 1)}>
            Fit
          </button>
          <button className="control" onClick={() => setDark((value) => !value)}>
            {dark ? 'Light' : 'Dark'}
          </button>
        </div>
      </header>

      <main className="workspace">
        <StatusBanner status={status} />
        <TopologyCanvas
          graph={graph}
          prepared={prepared}
          palette={palette}
          selected={selected}
          selectedEdge={selectedEdge}
          traceDepth={traceDepth}
          onSelect={(node: GraphNode | null) => {
            setSelected(node?.urn ?? null);
            if (node) {
              setSelectedEdge(null);
              if (narrow) setHostsOpen(false);
            }
          }}
          onSelectEdge={(key) => {
            setSelectedEdge(key);
            if (key) {
              setSelected(null);
              if (narrow) setHostsOpen(false);
            }
          }}
          inset={{
            top: topbarPad + (bannerVisible ? BANNER_HEIGHT : 0),
            // Sheets overlay the canvas on a phone — do not shrink fit into a strip.
            right: !narrow && inspectorOpen ? INSPECTOR_WIDTH : 0,
            bottom: legendPad,
            left: !narrow && (hostsOpen || activityOpen) ? HOSTS_WIDTH : 0,
          }}
          fitToken={fitToken}
          zoomToken={zoomToken}
          zoomFactor={zoomFactor}
          edgeHitPx={narrow || coarsePointer ? EDGE_HIT_PX_TOUCH : EDGE_HIT_PX}
        />
        {sheetOpen ? (
          <button
            type="button"
            className="sheet-backdrop"
            aria-label="Close panel"
            onClick={closeSheets}
          />
        ) : null}
        {hostsOpen ? (
          <HostsPanel
            fleet={fleet}
            providers={providers}
            controllerVersion={controllerVersion}
            resolveHostName={resolveHostName}
            onClose={() => setHostsOpen(false)}
          />
        ) : null}
        {activityOpen ? (
          <ActivityPanel
            activity={activity}
            nodes={graph.nodes}
            // A row is a way back to the node. Selecting it is what makes the
            // timeline part of the map rather than a log beside it.
            onSelect={(urn) => {
              setSelected(urn);
              setSelectedEdge(null);
              if (narrow) setActivityOpen(false);
            }}
            onClose={() => setActivityOpen(false)}
          />
        ) : null}
        {emptyExplanation ? <EmptyState explanation={emptyExplanation} /> : null}
        {emptyExplanation || sheetOpen ? null : <Legend palette={palette} />}
        {inspectorOpen ? (
          <NodeInspector
            node={selectedNode}
            edge={selectedLink}
            edges={selectedEdges}
            resolveName={resolveName}
            onClose={clearSelection}
            onNavigate={(urn) => {
              setSelected(urn);
              setSelectedEdge(null);
            }}
            actions={
              selectedNode ? (
                <ActionBar
                  actions={operations.actions}
                  phase={operations.phase}
                  busy={operations.busy}
                  name={selectedNode.name}
                  resolveName={resolveName}
                  onRun={(kind) => void runOperation(kind)}
                  onDismiss={operations.dismiss}
                />
              ) : null
            }
            logs={
              selectedNode ? <LogsPanel logs={logs} kind={selectedNode.kind} /> : null
            }
          />
        ) : null}
        <footer className="statusbar statusbar--hero">
          <span className="stat">
            <strong>{pad(counts.workloads)}</strong>
            <span className="stat__label"> services</span>
          </span>
          <span className="stat">
            <strong>{pad(counts.stacks)}</strong>
            <span className="stat__label"> stacks</span>
          </span>
          <span className="stat">
            <strong>{pad(counts.volumes)}</strong>
            <span className="stat__label"> volumes</span>
          </span>
          <span className="statusbar__hint">
            {counts.containers} containers · {counts.networks} networks discovered
          </span>
        </footer>
      </main>
    </div>
  );
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}
