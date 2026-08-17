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
  skewCount,
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
  /** Open this host's watch list. Per host, because the list is. */
  readonly onWatch: (engineId: string) => void;
  readonly onClose: () => void;
}

/** Relative times go stale silently; this is what keeps them honest. */
const TICK_MS = 30_000;

export function HostsPanel({
  fleet,
  providers,
  controllerVersion,
  resolveHostName,
  onWatch,
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
  const offVersion = skewCount(rows, controllerVersion);

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
        className="action action--primary hosts__add"
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

      {/* Once, above the list, and not on each card that differs: the command
          is the same on every one of them, and a constant repeated per row is
          how four hosts become four copies of one sentence. The cards say
          *which*; this says what to do about it.

          Shown verbatim from the Controller, which is the whole point. It
          carries the version the Controller is actually running, so upgrading
          the Controller changes this line without anyone editing it — and it
          has no token, which is the part an operator would get wrong from
          memory. */}
      {offVersion > 0 && fleet.terms ? (
        <UpgradeNotice
          count={offVersion}
          controllerVersion={controllerVersion}
          command={fleet.terms.upgrade}
        />
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
            onWatch={() => onWatch(row.agent.engine_id)}
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

/**
 * Hosts not on the Controller's version, and the line that fixes it.
 *
 * *Not* on it, rather than behind it: `versionSkew` compares strings and takes
 * no view on which number is larger, so this set contains the host somebody
 * upgraded before the Controller as readily as the four they have not got to.
 * Both want the same command — `install-agent.sh` installs the version this
 * Controller is composed from, in whichever direction that moves the host —
 * and calling a newer agent "older" is the kind of small lie an operator
 * notices and then stops trusting the rest of the panel over.
 *
 * A report and not an alarm. A mixed-version fleet is an ordinary operating
 * state under ADR-0008 — nothing here is broken, and every one of those hosts
 * is still managed — so this reads as a note in the panel rather than as an
 * error, and nothing about it blocks anything.
 *
 * The same `token__command` field the install command uses, for one reason:
 * that field is selectable and read-only, so an operator whose browser refuses
 * the clipboard (an insecure origin, which is exactly what a Controller on a
 * private IP is) can still select the text. A `<code>` block would look the
 * same and lose that.
 */
function UpgradeNotice({
  count,
  controllerVersion,
  command,
}: {
  count: number;
  controllerVersion: string | null;
  command: string;
}) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);

  // Same two seconds as `AddHostDialog`. A button that says "Copied" until the
  // panel closes stops being an answer about the click that just happened.
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2_000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  };

  return (
    <div className="hosts__upgrade">
      <p>
        {count === 1 ? '1 host is' : `${count} hosts are`} not on this Controller's
        version, which is {controllerVersion}. Run this on each of them, as root:
      </p>
      <div className="token">
        <input
          className="token__command"
          readOnly
          value={command}
          aria-label="Upgrade command"
          onFocus={(event) => event.currentTarget.select()}
        />
        <button type="button" className="control token__copy" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      {/* The sentence that saves an afternoon. Every other command in this
          panel carries a token, so its absence here looks like something the
          UI forgot rather than the thing that makes this an upgrade. */}
      <p className="hosts__upgrade-note">
        No token, deliberately: that is what keeps the host's identity instead of
        enrolling it again as a stranger. Nothing is deleted, and the host drops off the
        map for a second while the agent restarts.
      </p>
      {failed ? (
        <p className="hosts__upgrade-note">
          The browser would not write to the clipboard. Select the command above and copy
          it by hand.
        </p>
      ) : null}
    </div>
  );
}

function HostCard({
  row,
  now,
  controllerVersion,
  busy,
  confirming,
  onWatch,
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
  onWatch: () => void;
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
             carries no button: this card says *which* host differs, and the
             command that fixes it is named once at the top of the panel
             (`UpgradeNotice`) because it is the same on all of them.

             Both numbers, side by side, and no adjective between them. Which
             one is newer is a question `versionSkew` deliberately does not
             answer, and the pair reads correctly either way. */
          <>
            <span className="brand__sep">·</span>
            <span
              className="host__skew"
              title={`This Controller is ${controllerVersion}. The command that puts this host on it is at the top of this panel.`}
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
      {/* Above the enrollment buttons and present for every host that is
          managed at all, including this machine: what a host *shows on the
          map* is a different question from whether it is allowed to speak,
          and it is the one an operator returns to. */}
      {status === 'pending' || status === 'revoked' ? null : (
        <div className="actions host__actions">
          <button type="button" className="action" onClick={onWatch}>
            Services & processes
          </button>
        </div>
      )}

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
              className="action action--primary"
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
