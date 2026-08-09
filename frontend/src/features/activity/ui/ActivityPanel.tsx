/**
 * What has been done to this infrastructure, on the map that did it.
 *
 * Presentation only; every judgement is in `model/activity.ts`.
 *
 * This exists because a capability reachable only by `curl` is not finished.
 * `GET /commands/audit` was the last route in the API with no path through the
 * canvas, and the timeline is where the answer to "did that restart actually
 * work, and what else has happened here" belongs — next to the picture, not in
 * a terminal beside it.
 *
 * Each row is a way *back to the node*, not a line of text about it. Clicking
 * one selects its target on the canvas, which is what keeps this part of the
 * map rather than a log viewer that happens to share a window.
 */

import { useEffect, useState } from 'react';
import type { AuditEntry, Urn } from '../../../api/types';
import { ACTION_LABEL } from '../../operations/model/operations';
import {
  describeAge,
  describeDuration,
  describeEntry,
  fanOutOf,
  isReachable,
  statusWord,
  toneOf,
} from '../model/activity';
import type { Activity } from '../model/useActivity';

interface Props {
  readonly activity: Activity;
  /** The graph's nodes, to tell a target that still exists from one that does not. */
  readonly nodes: ReadonlyMap<Urn, { readonly name: string }>;
  readonly onSelect: (urn: Urn) => void;
  readonly onClose: () => void;
}

/** Relative times go stale silently. Same tick as the hosts panel. */
const TICK_MS = 30_000;

export function ActivityPanel({ activity, nodes, onSelect, onClose }: Props) {
  const now = useTick();

  return (
    <aside className="activity">
      <header className="hosts__head">
        <h2>Activity</h2>
        <button type="button" className="hosts__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      {activity.unreachable ? (
        <p className="hosts__blocked">
          The Controller is not answering, so this is the last list it gave.
        </p>
      ) : null}

      <div className="activity__list">
        {activity.entries.map((entry) => (
          <ActivityRow
            key={entry.id}
            entry={entry}
            now={now}
            nodes={nodes}
            onSelect={onSelect}
          />
        ))}

        {activity.entries.length === 0 && activity.loaded ? (
          <p className="hosts__empty">
            Nothing has been run yet. Operations appear here as they happen, including
            the ones the Controller refuses.
          </p>
        ) : null}
      </div>

      {/* Said once, at the bottom. The log survives a restart (ADR-0012 §4a);
          what it deliberately does not say is *who*, and a timeline read as an
          audit log would be trusted for exactly that. No longer "until
          authentication exists" — ADR-0014 decided it never will, which is
          also why nothing here can delete anything. Stating it as a permanent
          property rather than a pending one is the honest version: an operator
          who reads "until" plans around a date that is not coming. */}
      <p className="activity__caveat">
        Operations are not attributed — ByStack has no accounts, and no
        operation here can delete anything.
      </p>
    </aside>
  );
}

function ActivityRow({
  entry,
  now,
  nodes,
  onSelect,
}: {
  entry: AuditEntry;
  now: number;
  nodes: ReadonlyMap<Urn, { readonly name: string }>;
  onSelect: (urn: Urn) => void;
}) {
  const tone = toneOf(entry.status);
  const fanOut = fanOutOf(entry);
  const reachable = isReachable(entry.target, nodes);
  const name = nodes.get(entry.target)?.name ?? shortName(entry.target);
  const took = describeDuration(entry.duration_ms);

  return (
    <article className={`activity__row activity__row--${tone}`}>
      <header className="activity__head">
        <span className="activity__verb">{ACTION_LABEL[entry.kind] ?? entry.kind}</span>
        {reachable ? (
          <button
            type="button"
            className="activity__target"
            onClick={() => onSelect(entry.target)}
            title={entry.target}
          >
            {name}
          </button>
        ) : (
          // Not a dead button. A container recreated by `compose up` has a new
          // id, and the entry names the one that was killed — there is nothing
          // on the canvas to select, and a click that silently did nothing
          // would read as a broken panel.
          <span className="activity__target activity__target--gone" title={entry.target}>
            {name}
          </span>
        )}
        <span className={`activity__chip activity__chip--${tone}`}>
          {statusWord(entry.status)}
        </span>
      </header>

      <p className="activity__detail">{describeEntry(entry, fanOut)}</p>

      <p className="activity__meta">
        {describeAge(entry.at, now)}
        {fanOut > 1 ? (
          <>
            <span className="brand__sep">·</span>
            {fanOut} containers
          </>
        ) : null}
        {took ? (
          <>
            <span className="brand__sep">·</span>
            {took}
          </>
        ) : null}
        <span className="brand__sep">·</span>
        {entry.actor}
        {entry.reason ? (
          <>
            <span className="brand__sep">·</span>
            {entry.reason}
          </>
        ) : null}
      </p>
    </article>
  );
}

/** The last URN segment, shortened. Never the whole 64-character id. */
function shortName(urn: Urn): string {
  const last = urn.split('/').pop() ?? urn;
  return last.length > 14 ? `${last.slice(0, 12)}…` : last;
}

function useTick(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}
