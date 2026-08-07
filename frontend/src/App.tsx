/**
 * Application shell — canvas-first, DockGraph-style minimal chrome.
 */

import { useCallback, useMemo, useState } from 'react';
import type { GraphNode, Urn } from './api/types';
import { prepareTopologyGraph } from './features/topology/layout/prepareTopology';
import { neighborsOf } from './features/topology/model/graphStore';
import { deriveStatus, explainEmpty } from './features/topology/model/status';
import { useOperations } from './features/operations/model/useOperations';
import { ActionBar } from './features/operations/ui/ActionBar';
import { useGraphStream } from './features/topology/model/useGraphStream';
import { useHealth } from './features/topology/model/useHealth';
import { EmptyState, StatusBanner } from './features/topology/ui/CanvasOverlay';
import { Legend } from './features/topology/ui/Legend';
import { NodeInspector } from './features/topology/ui/NodeInspector';
import { TopologyCanvas } from './features/topology/ui/TopologyCanvas';
import { DARK, LIGHT } from './features/topology/ui/theme';

const API_BASE = import.meta.env.VITE_API_BASE ?? window.location.origin;

const INSPECTOR_WIDTH = 320;
/**
 * The legend plus the status bar under it, measured from the canvas bottom.
 * Keep in step with `.legend` in CSS — it is 107px tall and sits 42px up, and
 * the old 120 here let `fit` finish 11px underneath its first line.
 */
const LEGEND_HEIGHT = 150;
/** The topbar floats over a full-bleed canvas, so `fit` has to allow for it. */
const TOPBAR_HEIGHT = 58;
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

  const { graph, connection, resyncs } = useGraphStream(API_BASE);
  const health = useHealth(API_BASE);
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

  // Operations are scoped to whatever is selected. Only nodes the Controller
  // will act on get an action bar, and it decides which — not this component.
  const operations = useOperations(API_BASE, selected, graph.nodes);

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
  const providerLabel =
    providers.length > 1
      ? `${providers.length} sources`
      : (providers[0]?.id ??
        [...graph.nodes.values()].find((node) => node.kind === 'host')?.source ??
        'local');
  const readOnly = health.kind === 'reached' && health.health.read_only;

  const inspectorOpen = selectedNode !== null || selectedLink !== null;
  const bannerVisible = status.banner && status.detail !== null;

  return (
    <div className="app" data-theme={dark ? 'dark' : 'light'}>
      <header className="topbar topbar--slim">
        <div className="status-pill" title={status.detail ?? undefined}>
          <span className={`live-dot live-dot--${status.tone}`} />
          <span className="status-pill__text">
            {providerLabel}
            <span className="brand__sep">·</span>
            {status.label}
            {readOnly ? (
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

        <div className="brand brand--mark">ByStack</div>

        <input
          className="search"
          type="search"
          placeholder="Search…"
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
                  }}
                >
                  {node.name} <span className="muted">{node.kind}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <label className="control">
          Trace
          <input
            type="range"
            min={1}
            max={4}
            value={traceDepth}
            onChange={(event) => setTraceDepth(Number(event.target.value))}
          />
          <span>{traceDepth}</span>
        </label>

        <div className="spacer" />

        <button className="control" onClick={() => setFitToken((n) => n + 1)}>
          Fit
        </button>
        <button className="control" onClick={() => setDark((value) => !value)}>
          {dark ? 'Light' : 'Dark'}
        </button>
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
            if (node) setSelectedEdge(null);
          }}
          onSelectEdge={(key) => {
            setSelectedEdge(key);
            if (key) setSelected(null);
          }}
          inset={{
            top: TOPBAR_HEIGHT + (bannerVisible ? BANNER_HEIGHT : 0),
            right: inspectorOpen ? INSPECTOR_WIDTH : 0,
            bottom: LEGEND_HEIGHT,
          }}
          fitToken={fitToken}
        />
        {emptyExplanation ? <EmptyState explanation={emptyExplanation} /> : null}
        {emptyExplanation ? null : <Legend palette={palette} />}
        {inspectorOpen ? (
          <NodeInspector
            node={selectedNode}
            edge={selectedLink}
            edges={selectedEdges}
            resolveName={resolveName}
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
                  onRun={(kind) => void operations.run(kind)}
                  onDismiss={operations.dismiss}
                />
              ) : null
            }
          />
        ) : null}
        <footer className="statusbar statusbar--hero">
          <span className="stat">
            <strong>{pad(counts.workloads)}</strong> services
          </span>
          <span className="stat">
            <strong>{pad(counts.stacks)}</strong> stacks
          </span>
          <span className="stat">
            <strong>{pad(counts.volumes)}</strong> volumes
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
