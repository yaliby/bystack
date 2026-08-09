/**
 * The fleet, and the three things an operator does to it.
 *
 * Presentation. What a row *means* — whether a disconnected host is stale or
 * has simply never been seen, what order the list is in, what the revoke
 * confirmation says — is in `model/hosts.ts` and tested there.
 *
 * Two rules this file exists to honour:
 *
 * - Pending hosts come first and are styled apart, because the operator who
 *   opens this panel has usually just pasted an install command and is looking
 *   for exactly that row.
 * - There is no "approve all", and approval is never automatic here. A valid
 *   token still requiring a person is the second half of ADR-0011's security
 *   argument, and a button that approved a screenful would delete it.
 */

import { useEffect, useState } from 'react';
import type { JoinToken, ProviderHealth } from '../../../api/types';
import {
  LINK_LABEL,
  STATUS_LABEL,
  describeRow,
  describeSince,
  fleetRows,
  localAgentNotice,
  revokeWarning,
  versionSkew,
  type HostRow,
} from '../model/hosts';
import type { Fleet } from '../model/useFleet';
import { AddHostDialog } from './AddHostDialog';

interface Props {
  readonly fleet: Fleet;
  /** Provider health the app already polls — how `stale` is told from `never`. */
  readonly providers: readonly ProviderHealth[];
  /** What the Controller is running, so an agent's version means something. */
  readonly controllerVersion: string | null;
  /** The host node's name in the graph, when discovery has produced one. */
  readonly resolveHostName: (engineId: string) => string | null;
  readonly onClose: () => void;
}

/** Relative times go stale silently; this is what keeps them honest. */
const TICK_MS = 30_000;

export function HostsPanel({
  fleet,
  providers,
  controllerVersion,
  resolveHostName,
  onClose,
}: Props) {
  const [now, setNow] = useState(() => Date.now());
  const [token, setToken] = useState<JoinToken | null>(null);
  const [minting, setMinting] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  const rows = fleetRows(fleet.agents, providers, resolveHostName, now);
  const listenerOff = fleet.terms !== null && !fleet.terms.enabled;
  const localNotice = localAgentNotice(fleet.terms, rows);

  const addHost = async () => {
    setMinting(true);
    const minted = await fleet.mint();
    setMinting(false);
    if (minted) setToken(minted);
  };

  return (
    <aside className="hosts">
      <header className="hosts__head">
        <h2>Hosts</h2>
        <button type="button" className="hosts__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      <button
        type="button"
        className="action action--safe hosts__add"
        disabled={minting || listenerOff}
        onClick={() => void addHost()}
      >
        {minting ? 'Minting…' : 'Add host'}
      </button>

      {listenerOff ? (
        <p className="hosts__blocked">
          No agent listener is bound. A token would mint, and nothing would be there to
          redeem it. Set <code>agents.enabled: true</code> in <code>bystack.yaml</code> and
          restart the Controller; it will then listen on{' '}
          <code>{fleet.terms?.listen}</code>.
        </p>
      ) : null}

      {/* Above the list, because on a first run it is the *reason* the list
          and the canvas are both empty — and underneath them it would read as
          a footnote to a screen with nothing on it. */}
      {localNotice ? <p className="hosts__blocked">{localNotice}</p> : null}

      {fleet.error ? (
        <p className="hosts__error">
          {fleet.error}
          <button
            type="button"
            className="actions__dismiss"
            onClick={fleet.dismissError}
            aria-label="Dismiss"
          >
            ×
          </button>
        </p>
      ) : null}

      {fleet.unreachable ? (
        <p className="hosts__blocked">
          The Controller is not answering, so this list is the last one it gave.
        </p>
      ) : null}

      <div className="hosts__list">
        {rows.map((row) => (
          <HostCard
            key={row.agent.engine_id}
            row={row}
            now={now}
            controllerVersion={controllerVersion}
            busy={fleet.busy === row.agent.engine_id}
            confirming={confirming === row.agent.engine_id}
            onApprove={() => void fleet.approve(row.agent.engine_id)}
            onAskRevoke={() => setConfirming(row.agent.engine_id)}
            onCancelRevoke={() => setConfirming(null)}
            onRevoke={() => {
              setConfirming(null);
              void fleet.revoke(row.agent.engine_id);
            }}
          />
        ))}

        {rows.length === 0 && fleet.loaded ? (
          <p className="hosts__empty">
            No host has enrolled yet. Add one, and it will appear here as soon as its agent
            dials in.
          </p>
        ) : null}
      </div>

      {token ? (
        <AddHostDialog
          token={token}
          autoApprove={fleet.terms?.auto_approve ?? false}
          // Closing is what discards it. Nothing else holds a copy.
          onClose={() => setToken(null)}
        />
      ) : null}
    </aside>
  );
}

function HostCard({
  row,
  now,
  controllerVersion,
  busy,
  confirming,
  onApprove,
  onAskRevoke,
  onCancelRevoke,
  onRevoke,
}: {
  row: HostRow;
  now: number;
  controllerVersion: string | null;
  busy: boolean;
  confirming: boolean;
  onApprove: () => void;
  onAskRevoke: () => void;
  onCancelRevoke: () => void;
  onRevoke: () => void;
}) {
  const { agent, link, status } = row;
  const lastSeen = describeSince(agent.last_seen, now);

  return (
    <article className={`host host--${status} host--link-${link.kind}`}>
      <header className="host__head">
        <span className="host__name" title={agent.engine_id}>
          {row.label}
        </span>
        {/* Two chips, never one. `status` is what a person decided about this
            host; the link is what the network is doing right now. Approved and
            asleep, or revoked and still streaming, are both ordinary. */}
        <span className={`host__chip host__chip--${status}`}>{STATUS_LABEL[status]}</span>
        {/* A third chip only where it says something the first two do not: a
            host that is enrolled *and* running the Controller's own agent. On
            an unenrolled one the status chip already reads "This machine". */}
        {agent.local && status !== 'local' ? (
          <span className="host__chip host__chip--local">{STATUS_LABEL.local}</span>
        ) : null}
        <span className={`host__chip host__chip--link-${link.kind}`}>
          {LINK_LABEL[link.kind]}
        </span>
      </header>

      <p className="host__detail">{describeRow(row, lastSeen)}</p>

      {row.note ? <p className="host__note">{row.note}</p> : null}

      <p className="host__meta">
        {agent.agent_version ? `agent ${agent.agent_version}` : 'version unknown'}
        {versionSkew(agent, controllerVersion) ? (
          /* A report, not a fault. A mixed-version fleet is an ordinary
             operating state under ADR-0008, so this is styled as a note and
             carries no button: upgrading is `install-agent.sh` on that host
             (ADR-0015). What it replaces is a version printed next to nothing
             to compare it against. */
          <>
            <span className="brand__sep">·</span>
            <span
              className="host__skew"
              title={`This Controller is ${controllerVersion}. Re-run install-agent.sh on that host to upgrade it.`}
            >
              Controller is {controllerVersion}
            </span>
          </>
        ) : null}
        <span className="brand__sep">·</span>
        {status === 'local'
          ? `connected ${describeSince(agent.enrolled_at, now) ?? 'just now'}`
          : `enrolled ${describeSince(agent.enrolled_at, now) ?? 'recently'}`}
      </p>

      {/* No buttons for this machine. There is no enrollment record behind it,
          so approve would have nothing to change and revoke would have nothing
          to refuse — both would return 404 and read as a broken panel. The way
          to stop managing it is `local_agent.enabled`, which the row says. */}
      {status === 'local' ? null : confirming ? (
        <div className="host__confirm">
          <p>{revokeWarning(row)}</p>
          <div className="actions">
            <button type="button" className="action action--destructive" onClick={onRevoke}>
              Revoke {row.label}
            </button>
            <button type="button" className="action action--safe" onClick={onCancelRevoke}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="actions host__actions">
          {status === 'pending' ? (
            <button
              type="button"
              className="action action--safe"
              disabled={busy}
              onClick={onApprove}
            >
              Approve
            </button>
          ) : null}
          {status === 'revoked' ? null : (
            <button
              type="button"
              className="action action--disruptive"
              disabled={busy}
              onClick={onAskRevoke}
            >
              Revoke
            </button>
          )}
        </div>
      )}
    </article>
  );
}
