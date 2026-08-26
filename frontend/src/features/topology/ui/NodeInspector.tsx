/**
 * Detail panel for the selected node or edge.
 *
 * Presentation only. It receives a node/edge and renders them; it does not
 * fetch, derive or decide anything.
 */

import type { GraphEdge, GraphNode, Urn } from '../../../api/types';
import { EDGE_LABEL, KIND_HINT, KIND_LABEL, edgeVerb, statusOf, statusText } from './theme';

interface Props {
  readonly node: GraphNode | null;
  readonly edge?: GraphEdge | null;
  readonly edges: readonly GraphEdge[];
  readonly resolveName: (urn: Urn) => string;
  readonly onNavigate: (urn: Urn) => void;
  /** Clears selection — needed on a phone sheet so the canvas can reclaim the screen. */
  readonly onClose?: () => void;
  /**
   * The operations bar, passed in rather than built here.
   *
   * This component stays presentation-only: it renders what it is given and
   * fetches nothing. Reaching for the command API from inside it would put a
   * network call behind every selection change in the least testable place
   * in the app.
   */
  readonly actions?: React.ReactNode;
  /**
   * The log panel, passed in for the same reason as `actions`.
   *
   * Kept a slot rather than a prop bundle so this component still fetches
   * nothing: reading a log is a round trip to an agent on a remote host, and
   * putting that behind a selection change in the least testable file in the
   * app is exactly what this separation exists to prevent.
   */
  readonly logs?: React.ReactNode;
}

export function NodeInspector({
  node,
  edge = null,
  edges,
  resolveName,
  onNavigate,
  onClose,
  actions = null,
  logs = null,
}: Props) {
  if (edge && !node) {
    return (
      <EdgeInspector
        edge={edge}
        resolveName={resolveName}
        onNavigate={onNavigate}
        onClose={onClose}
      />
    );
  }

  if (!node) {
    return (
      <aside className="inspector inspector--empty">
        {onClose ? (
          <button type="button" className="inspector__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        ) : null}
        <p>Select a node or edge to inspect it.</p>
        <p className="muted">Click a link · drag a card to play.</p>
      </aside>
    );
  }

  const status = statusOf(node);

  return (
    <aside className="inspector">
      <header className="inspector__head">
        <div className="inspector__head-main">
          <span className="kind-chip">{KIND_LABEL[node.kind]}</span>
          <h2>{node.name}</h2>
          <p className={`status status--${status}`}>
            <span className="status-dot" aria-hidden="true" />
            {statusText(node)}
          </p>
          {/* The chip names the kind; this says what the kind is. Without it,
              "Compose service" and "systemd unit" are the same word to anyone
              who has not read the model. */}
          <p className="kind-hint">{KIND_HINT[node.kind]}</p>
        </div>
        {onClose ? (
          <button type="button" className="inspector__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        ) : null}
      </header>

      {/* The panel no longer scrolls itself: its corner marks are drawn on the
          panel, and an element that scrolls its own children would carry them
          off the bottom of the content. */}
      <div className="inspector__body">
        {/* Above identity and attributes: during an incident the operator is
            here to act, not to read a URN. */}
        {actions}

        {/* Directly under the actions and above identity: an operator who has
            just been offered `restart` is here because something is wrong, and
            the log is how they decide whether to press it. */}
        {logs}

        <Section title="Identity">
          <Row label="URN" value={node.urn} mono />
          <Row label="Discovered by" value={node.source} />
        </Section>

        {Object.keys(node.attrs).length > 0 && (
          <Section title="Attributes">
            {Object.entries(node.attrs)
              .filter(([, value]) => value !== null && value !== undefined && value !== '')
              .map(([key, value]) => (
                <Row key={key} label={key} value={format(value)} />
              ))}
          </Section>
        )}

        {Object.keys(node.labels).length > 0 && (
          <Section title="Labels">
            {Object.entries(node.labels).map(([key, value]) => (
              <Row key={key} label={key} value={value} mono />
            ))}
          </Section>
        )}

        <Section title={`Relationships (${edges.length})`}>
          {edges.map((rel) => {
            const outward = rel.src === node.urn;
            const other = outward ? rel.dst : rel.src;
            return (
              <button
                key={rel.key}
                type="button"
                className="relationship"
                onClick={() => onNavigate(other)}
              >
                <span className="relationship__body">
                  <span className="relationship__verb">
                    {edgeVerb(rel.kind, outward ? 'out' : 'in')}
                  </span>
                  <span className="relationship__target">{resolveName(other)}</span>
                </span>
                <span className="relationship__go" aria-hidden="true">
                  →
                </span>
              </button>
            );
          })}
        </Section>
      </div>
    </aside>
  );
}

function EdgeInspector({
  edge,
  resolveName,
  onNavigate,
  onClose,
}: {
  edge: GraphEdge;
  resolveName: (urn: Urn) => string;
  onNavigate: (urn: Urn) => void;
  onClose?: () => void;
}) {
  return (
    <aside className="inspector">
      <header className="inspector__head">
        <div className="inspector__head-main">
          <span className={`kind-chip kind-chip--edge kind-chip--${edge.kind}`}>
            {EDGE_LABEL[edge.kind]}
          </span>
          <h2>Link</h2>
          <p className="muted edge-flow">
            <button type="button" className="linkish" onClick={() => onNavigate(edge.src)}>
              {resolveName(edge.src)}
            </button>
            <span aria-hidden="true"> → </span>
            <button type="button" className="linkish" onClick={() => onNavigate(edge.dst)}>
              {resolveName(edge.dst)}
            </button>
          </p>
        </div>
        {onClose ? (
          <button type="button" className="inspector__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        ) : null}
      </header>

      <div className="inspector__body">
        <Section title="Identity">
          <Row label="Kind" value={EDGE_LABEL[edge.kind]} />
          <Row label="Key" value={edge.key} mono />
          <Row label="From" value={resolveName(edge.src)} />
          <Row label="To" value={resolveName(edge.dst)} />
        </Section>

        {Object.keys(edge.attrs).length > 0 && (
          <Section title="Attributes">
            {Object.entries(edge.attrs)
              .filter(([, value]) => value !== null && value !== undefined && value !== '')
              .map(([key, value]) => (
                <Row key={key} label={key} value={format(value)} />
              ))}
          </Section>
        )}

        <Section title="Navigate">
          <button type="button" className="relationship" onClick={() => onNavigate(edge.src)}>
            <span className="relationship__body">
              <span className="relationship__verb">source</span>
              <span className="relationship__target">{resolveName(edge.src)}</span>
            </span>
            <span className="relationship__go" aria-hidden="true">
              →
            </span>
          </button>
          <button type="button" className="relationship" onClick={() => onNavigate(edge.dst)}>
            <span className="relationship__body">
              <span className="relationship__verb">target</span>
              <span className="relationship__target">{resolveName(edge.dst)}</span>
            </span>
            <span className="relationship__go" aria-hidden="true">
              →
            </span>
          </button>
        </Section>
      </div>
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="inspector__section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="row">
      <span className="row__label">{label}</span>
      <span className={mono ? 'row__value row__value--mono' : 'row__value'}>{value}</span>
    </div>
  );
}

function format(value: unknown): string {
  if (Array.isArray(value)) return value.map(format).join(', ');
  if (value && typeof value === 'object') {
    // A port binding is the one attribute shape the map already has words for
    // (`cardPorts`, `edgePortLabel`). Spelling it `{"public":443,...}` here
    // makes the inspector the only place in the app that talks like Docker.
    const row = value as Record<string, unknown>;
    if (row.public != null && row.private != null) return `:${row.public} → ${row.private}`;
    return JSON.stringify(value);
  }
  return String(value);
}
