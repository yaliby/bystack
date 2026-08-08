/**
 * Detail panel for the selected node or edge.
 *
 * Presentation only. It receives a node/edge and renders them; it does not
 * fetch, derive or decide anything.
 */

import type { GraphEdge, GraphNode, Urn } from '../../../api/types';
import { EDGE_LABEL, KIND_LABEL, STATUS_LABEL, statusOf } from './theme';

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
}

export function NodeInspector({
  node,
  edge = null,
  edges,
  resolveName,
  onNavigate,
  onClose,
  actions = null,
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
            {STATUS_LABEL[status]}
            {node.status && status !== 'neutral' ? ` (${node.status})` : ''}
          </p>
        </div>
        {onClose ? (
          <button type="button" className="inspector__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        ) : null}
      </header>

      {/* Above identity and attributes: during an incident the operator is
          here to act, not to read a URN. */}
      {actions}

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
          const outgoing = rel.src === node.urn;
          const other = outgoing ? rel.dst : rel.src;
          return (
            <button key={rel.key} className="relationship" onClick={() => onNavigate(other)}>
              <span className="relationship__verb">
                {outgoing ? '→' : '←'} {EDGE_LABEL[rel.kind]}
              </span>
              <span className="relationship__target">{resolveName(other)}</span>
            </button>
          );
        })}
      </Section>
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
        <button className="relationship" onClick={() => onNavigate(edge.src)}>
          <span className="relationship__verb">← source</span>
          <span className="relationship__target">{resolveName(edge.src)}</span>
        </button>
        <button className="relationship" onClick={() => onNavigate(edge.dst)}>
          <span className="relationship__verb">→ target</span>
          <span className="relationship__target">{resolveName(edge.dst)}</span>
        </button>
      </Section>
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
  if (value && typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
