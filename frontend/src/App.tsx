/**
 * Application shell — canvas-first, DockGraph-style minimal chrome.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CommandKind, GraphNode, GroupCommandResult, Urn } from './api/types';
import { prepareTopologyGraph } from './features/topology/layout/prepareTopology';
import { neighborsOf } from './features/topology/model/graphStore';
import { deriveStatus, explainEmpty } from './features/topology/model/status';
import { pendingCount } from './features/hosts/model/hosts';
import { useController } from './features/hosts/model/useController';
import { useFleet } from './features/hosts/model/useFleet';
import { HostsPanel } from './features/hosts/ui/HostsPanel';
import { useActivity } from './features/activity/model/useActivity';
import { ActivityPanel } from './features/activity/ui/ActivityPanel';
import { WatchPanel } from './features/watch/ui/WatchPanel';
import { useWatch } from './features/watch/model/useWatch';
import { useGroups } from './features/watch/model/useGroups';
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
import { isMockMode } from './mock/demoMode';

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
/** Measured, not guessed: the narrow legend is three wrapped columns, 148px. */
const LEGEND_HEIGHT_NARROW = 148;
/** The topbar floats over a full-bleed canvas, so `fit` has to allow for it. */
const TOPBAR_HEIGHT = 58;
/**
 * The plate wraps on a phone — identity, actions, search, trace — and the
 * tallest it gets is three rows ending at 169px, measured. The rest is the
 * same clearance the wide value carries over its own 43px plate.
 */
const TOPBAR_HEIGHT_NARROW = 184;
/** The trace rail's seats. Four values, so four seats — never a slider. */
const TRACE_DEPTHS = [1, 2, 3, 4] as const;
/** So does the banner, when there is one. Keep in step with `.banner` in CSS. */
const BANNER_HEIGHT = 33;
/** Docker's own networks. Present on every host, so counting them says nothing. */
const DEFAULT_NETWORKS = new Set(['bridge', 'host', 'none']);
/**
 * What the headline figure calls a workload.
 *
 * A watched unit and a watched process are drawn as containers and operated as
 * containers, so leaving them out made the number disagree with the cards the
 * operator can actually count on the canvas.
 */
const WORKLOAD_KINDS = new Set(['service', 'container', 'unit', 'process']);

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
  // Which host's selection is being edited, if any. One at a time and by
  // engine id, because the watch list is per host and there is no such thing
  // as editing the fleet's.
  const [watchingHost, setWatchingHost] = useState<string | null>(null);
  const narrow = useMediaQuery(NARROW_QUERY);
  const coarsePointer = useMediaQuery(COARSE_POINTER_QUERY);
  const searchRef = useRef<HTMLInputElement>(null);

  const { graph, connection, resyncs } = useGraphStream(API_BASE);
  const health = useHealth(API_BASE);
  // Polled faster while the panel is open: a host that has just been given the
  // install command is the thing the operator is watching for.
  const fleet = useFleet(API_BASE, hostsOpen);
  // Not gated on the panel being open, unlike `useFleet`. An update outlives
  // whatever the operator does with the sheet -- it stops and replaces the
  // process serving this page -- and a hook that only polled while a panel was
  // open would forget a run the moment somebody went back to the map to wait
  // it out. Thirty seconds when nothing is happening; two while one is.
  const controller = useController(API_BASE);
  const activity = useActivity(API_BASE, activityOpen);
  // Loaded when a host is chosen and at no other time. Nothing here polls:
  // the list changes when this operator changes it, and the *states* arrive on
  // the delta stream like every other node.
  const watching = useWatch(API_BASE, watchingHost);
  // Fleet-wide, so a card on the map knows which other machines were chosen
  // alongside it. Refetched when this operator edits a list and at no other
  // time — see `useGroups`.
  const [watchNonce, setWatchNonce] = useState(0);
  const [groupResult, setGroupResult] = useState<GroupCommandResult | null>(null);
  // Cards Choose / All would reach — lit on the canvas until the scope
  // collapses back to this host or the selection moves.
  const [affecting, setAffecting] = useState<readonly Urn[]>([]);
  const palette = dark ? DARK : LIGHT;
  const mockCanvas = isMockMode();

  // Mock invents a live socket so the canvas can paint. Surfacing that as
  // "live" next to a separate "MOCK" badge is two answers to one question.
  const status = useMemo(() => {
    if (mockCanvas) {
      return {
        tone: 'warn' as const,
        label: 'mock',
        detail:
          'Canned demo fleet — three hosts, watched units/processes, and group scope. Not a Controller.',
        banner: false,
        ailing: [] as const,
      };
    }
    return deriveStatus(connection, health);
  }, [mockCanvas, connection, health]);

  // Also on the document element, not only on the shell below. The join-token
  // dialog is portalled to `body` to escape a `backdrop-filter` containing
  // block, and a theme scoped to `.app` would leave it with no colours at all.
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  }, [dark]);

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

  // Declared after `resolveHostName` because it needs it: a group's members
  // are engine ids, and an engine id is not a name.
  const groups = useGroups(API_BASE, watchNonce, resolveHostName);

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
   * The same, for a command aimed at more than one host.
   *
   * One audit entry per host, so the timeline gains N rows rather than one —
   * which is the truth: these are N operations on N machines that happened to
   * be asked for together.
   */
  const runGroupOperation = useCallback(
    async (kind: CommandKind, engineIds: readonly string[]) => {
      const target = groups.byUrn.get(selected ?? ('' as Urn));
      if (!target) return;
      setGroupResult(await groups.run(kind, target.groupId, engineIds));
      activity.refresh();
    },
    [groups, selected, activity],
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
      if (WORKLOAD_KINDS.has(node.kind)) workloads += 1;
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
        null);
  // Never on the mock canvas: the canned health answer is read_only so the
  // demo cannot pretend to mutate, but that is an implementation detail — not
  // an operating mode the operator chose.
  const readOnly = !mockCanvas && health.kind === 'reached' && health.health.read_only;
  const pending = pendingCount(fleet.agents);

  const inspectorOpen = selectedNode !== null || selectedLink !== null;
  const bannerVisible = status.banner && status.detail !== null;
  const sheetOpen = narrow && (hostsOpen || activityOpen || inspectorOpen);

  const clearSelection = useCallback(() => {
    setSelected(null);
    setSelectedEdge(null);
    setAffecting([]);
  }, []);

  const marked = useMemo(
    () => (affecting.length > 0 ? new Set<Urn>(affecting) : null),
    [affecting],
  );

  const onAffecting = useCallback((urns: readonly Urn[]) => {
    setAffecting((prev) => {
      if (prev.length === urns.length && prev.every((urn, i) => urn === urns[i])) return prev;
      return urns;
    });
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

  // Escape dismisses floating chrome the same way the sheet backdrop does, and
  // `/` reaches the search without the mouse. Both are on the window because
  // the canvas holds focus for most of a session and neither should require
  // finding the chrome first.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === '/' && !event.metaKey && !event.ctrlKey && !event.altKey) {
        // Not while someone is typing — in a field, `/` is a character.
        const active = document.activeElement;
        const tag = active?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || (active as HTMLElement)?.isContentEditable) {
          return;
        }
        event.preventDefault();
        searchRef.current?.focus();
        return;
      }
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
      <header className="topbar">
        {/* One plate. Separate floating capsules for brand, env, search and
            trace is how a header turns into a row of badges. */}
        <div className="topbar__plate">
          <div className="topbar__identity" title={status.detail ?? undefined}>
            <span className="brand">
              <BrandMark />
              <span className="brand__word">ByStack</span>
            </span>
            <span className="status-readout">
              <span className={`live-dot live-dot--${status.tone}`} />
              <span className="status-readout__text">
                {mockCanvas || narrow || !providerLabel ? (
                  status.label
                ) : (
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
                {!mockCanvas && resyncs > 0 ? (
                  <>
                    <span className="brand__sep">·</span>
                    {resyncs} resync
                  </>
                ) : null}
              </span>
            </span>
          </div>

          <div className="topbar__search">
            <input
              ref={searchRef}
              className="search"
              type="search"
              placeholder="Search the map…"
              title="Press / to focus"
              enterKeyHint="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.stopPropagation();
                  setQuery('');
                  event.currentTarget.blur();
                }
              }}
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

          <div className="spacer" />

          {/* Depth is a hop count, not a tab strip. The active value is weight;
              the inactive ones stay quiet text. */}
          <div className="trace" role="group" aria-label="Trace depth">
            <span className="trace__label" aria-hidden="true">
              Trace
            </span>
            {TRACE_DEPTHS.map((depth) => (
              <button
                key={depth}
                type="button"
                className="trace__step"
                aria-pressed={traceDepth === depth}
                aria-label={`Trace ${depth} ${depth === 1 ? 'hop' : 'hops'}`}
                onClick={() => setTraceDepth(depth)}
              >
                {depth}
              </button>
            ))}
          </div>

          <div className="topbar__actions">
            {/* The count rides on the closed button on purpose: a host that has
                just enrolled is waiting on a person, and it should not need the
                panel to be open to say so. */}
            <button
              type="button"
              className={`topbar__btn${pending > 0 ? ' topbar__btn--attention' : ''}`}
              aria-pressed={hostsOpen}
              onClick={openHosts}
            >
              Hosts
              {pending > 0 ? <span className="topbar__count">{pending}</span> : null}
            </button>
            <button
              type="button"
              className="topbar__btn"
              aria-pressed={activityOpen}
              onClick={openActivity}
            >
              Activity
            </button>
            <span className="topbar__rule" aria-hidden="true" />
            {narrow ? (
              <>
                <button
                  type="button"
                  className="topbar__btn topbar__btn--icon"
                  aria-label="Zoom out"
                  onClick={() => bumpZoom(1 / ZOOM_BUTTON_FACTOR)}
                >
                  −
                </button>
                <button
                  type="button"
                  className="topbar__btn topbar__btn--icon"
                  aria-label="Zoom in"
                  onClick={() => bumpZoom(ZOOM_BUTTON_FACTOR)}
                >
                  +
                </button>
              </>
            ) : null}
            <button
              type="button"
              className="topbar__btn"
              onClick={() => setFitToken((n) => n + 1)}
            >
              Fit
            </button>
            <button
              type="button"
              className="topbar__btn topbar__btn--icon"
              aria-label={dark ? 'Switch to the light theme' : 'Switch to the dark theme'}
              title={dark ? 'Light theme' : 'Dark theme'}
              onClick={() => setDark((value) => !value)}
            >
              {dark ? <SunIcon /> : <MoonIcon />}
            </button>
          </div>
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
          marked={marked}
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
            controller={controller}
            providers={providers}
            controllerVersion={controllerVersion}
            resolveHostName={resolveHostName}
            onWatch={(engineId) => setWatchingHost(engineId)}
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
        {watchingHost ? (
          <WatchPanel
            baseUrl={API_BASE}
            engineId={watchingHost}
            hostName={resolveHostName(watchingHost) ?? watchingHost.slice(0, 12)}
            watching={watching}
            nodes={graph.nodes}
            // The fleet, so one selection can reach more than one machine.
            // Already polled for the hosts panel; the picker reads the same
            // list rather than asking for its own.
            hosts={fleet.agents}
            resolveHostName={resolveHostName}
            // An edit here is the only thing that changes a group, so it is
            // the only thing that has to invalidate the fleet-wide index.
            // Nothing polls for it.
            onChanged={() => setWatchNonce((n) => n + 1)}
            onSelect={(urn) => {
              setSelected(urn);
              setSelectedEdge(null);
              if (narrow) setWatchingHost(null);
            }}
            onClose={() => setWatchingHost(null)}
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
                  // Null for anything that was not chosen alongside other
                  // machines, which is most of the map: a container has no
                  // watch group at all, and a service chosen for one host is
                  // a group of one. The bar offers a scope only when there is
                  // genuinely more than one machine to choose between.
                  group={selected ? (groups.byUrn.get(selected) ?? null) : null}
                  groupBusy={groups.busy}
                  groupResult={groupResult}
                  groupError={groups.error}
                  onRunGroup={(kind, engineIds) => void runGroupOperation(kind, engineIds)}
                  onDismissGroup={() => {
                    setGroupResult(null);
                    groups.dismissError();
                  }}
                  onAffecting={onAffecting}
                />
              ) : null
            }
            logs={
              selectedNode ? <LogsPanel logs={logs} kind={selectedNode.kind} /> : null
            }
          />
        ) : null}
        <footer className="statusbar">
          <span className="stat">
            <Reading value={counts.workloads} />
            <span className="stat__label">services</span>
          </span>
          <span className="stat">
            <Reading value={counts.stacks} />
            <span className="stat__label">stacks</span>
          </span>
          <span className="stat">
            <Reading value={counts.volumes} />
            <span className="stat__label">volumes</span>
          </span>
          <span className="statusbar__hint">
            {counts.containers} containers · {counts.networks} networks discovered
          </span>
        </footer>
      </main>
    </div>
  );
}

/**
 * One figure off the canvas, padded to a fixed column.
 *
 * The pad is scaffolding and is drawn as such: it keeps `03` and `12` the same
 * width so the row does not shuffle every time a container comes up, without
 * `03` reading as a quantity somebody wrote down.
 */
function Reading({ value }: { value: number }) {
  const text = String(value);
  const pad = text.length < 2 ? '0'.repeat(2 - text.length) : '';
  return (
    <span className="stat__num">
      {pad ? <span className="stat__pad">{pad}</span> : null}
      {text}
    </span>
  );
}

/**
 * The wordmark's glyph — a frame with two linked cards in it.
 *
 * Deliberately the canvas's own grammar rather than a logo: a stack frame, two
 * nodes and the link between them is the whole of what this product draws.
 */
function BrandMark() {
  return (
    <svg
      className="brand__mark"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <rect
        x="1.3"
        y="1.3"
        width="13.4"
        height="13.4"
        rx="4"
        stroke="currentColor"
        strokeWidth="1.2"
        opacity="0.45"
      />
      <path d="M6.5 6.5 9.5 9.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      <circle cx="5.3" cy="5.3" r="1.75" fill="currentColor" />
      <circle cx="10.7" cy="10.7" r="1.75" fill="currentColor" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="3.1" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M8 1.2v1.6M8 13.2v1.6M14.8 8h-1.6M2.8 8H1.2M12.8 3.2l-1.1 1.1M4.3 11.7l-1.1 1.1M12.8 12.8l-1.1-1.1M4.3 4.3 3.2 3.2"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M13.4 9.8A5.9 5.9 0 0 1 6.2 2.6a5.9 5.9 0 1 0 7.2 7.2Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}
