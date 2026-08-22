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
import {
  controllerOffer,
  describeUpdate,
  progressOf,
  updateFailed,
  type ControllerOffer,
} from '../model/controller';
import {
  describeRollout,
  rolloutFailed,
  rolloutRows,
  upgradeOffer,
  type UpgradeOffer,
} from '../model/upgrade';
import type { Controller } from '../model/useController';
import type { Fleet } from '../model/useFleet';
import type { Deployment } from '../model/useDeploy';
import { AddHostDialog } from './AddHostDialog';

interface Props {
  readonly fleet: Fleet;
  /**
   * This Controller, and the one button that replaces it (ADR-0018).
   *
   * On the Hosts panel rather than in a settings screen, because it is the
   * first half of one operation: updating the Controller is what makes the
   * fleet behind it upgradable, and the manager fetches the agents for the
   * version it just installed. Two buttons in two places for one intention is
   * how half a fleet ends up on a version nobody chose.
   */
  readonly controller: Controller;
  /**
   * Installing an agent on a machine that does not have one (ADR-0019).
   *
   * Here rather than inside `AddHostDialog` so a run survives the dialog being
   * closed: the credential does not, and must not, but the *progress* is the
   * only view an operator has of an install that is still going.
   */
  readonly deployment: Deployment;
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

/**
 * Whether this page was served from the machine it is managing.
 *
 * Read from the browser rather than from the Controller on purpose: what the
 * warning in the add-host dialog is about is the network *this request* just
 * crossed, and the Controller cannot see that. A dashboard reached over
 * `ssh -L` is `127.0.0.1` here and is genuinely as safe as the operator's own
 * SSH session, which is exactly the case a server-side answer would get wrong.
 */
function isLoopback(): boolean {
  const host = window.location.hostname;
  return host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]';
}

export function HostsPanel({
  fleet,
  controller,
  deployment,
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
  const [refusal, setRefusal] = useState<string | null>(null);
  const [selfRefusal, setSelfRefusal] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  const rows = fleetRows(fleet.agents, providers, resolveHostName, now);
  const listenerOff = fleet.terms !== null && !fleet.terms.enabled;
  const localNotice = localAgentNotice(fleet.terms, rows);
  const offVersion = skewCount(rows, controllerVersion);
  const offer = upgradeOffer(fleet.agents, controllerVersion, fleet.releases, fleet.rollout);
  const self = controllerOffer(controller.self);

  const startUpgrade = async (version: string) => {
    setRefusal(null);
    const refused = await fleet.upgrade(version);
    if (refused) setRefusal(refused);
  };

  const updateController = async () => {
    setSelfRefusal(null);
    const refused = await controller.update();
    if (refused) setSelfRefusal(refused);
  };

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

      {/* Once, above the list, and not on each card that differs. The cards
          say *which* hosts; this says what to do about them, and what that is
          depends on this Controller rather than on a preference: one holding a
          signed release pushes it (ADR-0017), and one holding nothing hands
          out the installer line (ADR-0015). A rollout in flight replaces both,
          because an operator looking at this panel during one is asking about
          it. */}
      <Upgrade
        offer={offer}
        count={offVersion}
        controllerVersion={controllerVersion}
        command={fleet.terms?.upgrade ?? null}
        refusal={refusal}
        onStart={startUpgrade}
        onStop={() => void fleet.stopUpgrade()}
        onDismiss={() => {
          setRefusal(null);
          fleet.dismissRollout();
        }}
      />

      <ControllerUpdate
        offer={self}
        refusal={selfRefusal}
        onUpdate={() => void updateController()}
        onDismiss={() => {
          setSelfRefusal(null);
          controller.dismiss();
        }}
      />

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
          deployment={deployment}
          // `read_only` is the Controller's answer, and it is the same field
          // that refuses the route -- so the form says why rather than
          // submitting into a 409.
          canDeploy={!(controller.self?.read_only ?? false)}
          // Whether the credential typed below would leave this machine. The
          // Controller reports where it is bound; a dashboard published on a
          // network has no login in front of it (ADR-0014), and this is the
          // one form where that matters enough to say out loud.
          loopback={isLoopback()}
          // Closing is what discards the token. Nothing else holds a copy.
          onClose={() => setToken(null)}
        />
      ) : null}
    </aside>
  );
}

/**
 * Updating the Controller this dashboard is served by (ADR-0018).
 *
 * The one component in the app that has to be correct about **its own backend
 * going away mid-operation**. Between `applying` and the next successful poll,
 * every request from this page fails, and none of those failures is an error:
 * they are the update working. `useController` swallows them and leaves the
 * last answer on screen, so what is drawn here during that window is the
 * `applying` phase — which is exactly what is true.
 *
 * The bar's percentage is invented here (`progressOf`) and nowhere else. The
 * Controller publishes named steps and no fraction, deliberately; turning six
 * steps into a number is a presentation decision, and keeping it on this side
 * is what stops it becoming a second model of the update that can disagree
 * with the first.
 */
function ControllerUpdate({
  offer,
  refusal,
  onUpdate,
  onDismiss,
}: {
  offer: ControllerOffer | null;
  refusal: string | null;
  onUpdate: () => void;
  onDismiss: () => void;
}) {
  // No claim until the first answer, and nothing at all where there is no
  // updater: on a container or a checkout this whole section is absent rather
  // than being a disabled button with an explanation nobody asked for. The
  // reason is still on `GET /controller` for anyone who does.
  if (offer === null || offer.kind === 'unavailable') return null;

  if (offer.kind === 'idle') {
    return (
      <div className="hosts__self">
        <p className="hosts__self-version">
          This Controller is running <strong>{offer.version}</strong>.
        </p>
        <div className="actions">
          <button type="button" className="action action--primary" onClick={onUpdate}>
            Update system
          </button>
        </div>
        <p className="hosts__upgrade-note">
          Pulls the newest signed release, replaces this Controller and puts the previous one
          back if it does not come up. The fleet is rolled forward afterwards, one host at a
          time.
        </p>
        {refusal ? <p className="hosts__error">{refusal}</p> : null}
      </div>
    );
  }

  const { update } = offer;
  const failed = updateFailed(update);
  const percent = Math.round(progressOf(update) * 100);

  return (
    <div className={`hosts__self${failed ? ' hosts__self--failed' : ''}`}>
      <p className="hosts__self-version">
        {offer.kind === 'running' ? `Updating to ${update.version}` : `Update ${update.phase}`}
      </p>

      {offer.kind === 'running' ? (
        <div
          className="hosts__self-bar"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          aria-label={`Updating this Controller to ${update.version}`}
        >
          <span className="hosts__self-fill" style={{ width: `${percent}%` }} />
        </div>
      ) : null}

      <p className="hosts__upgrade-note">{describeUpdate(update)}</p>

      {update.cascade ? (
        <p className="hosts__upgrade-note">
          The fleet is being rolled forward to {update.cascade} below.
        </p>
      ) : null}

      {refusal ? <p className="hosts__error">{refusal}</p> : null}

      {/* No cancel button, and that is not an omission. Once the swap has
          started there is nothing safe to stop: the process that would have to
          honour the request is the one being replaced, and the machine already
          undoes a swap that does not come up. A button that could only be
          pressed too early or too late is worse than none. */}
      {offer.kind === 'ended' ? (
        <div className="actions">
          <button type="button" className="action action--safe" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The one thing to do about a fleet that is not on the Controller's version.
 *
 * A switch over `UpgradeOffer` and nothing else, because the four cases want
 * four different paragraphs and the decision between them is in
 * `model/upgrade.ts` where it is tested. A component that took five booleans
 * would reliably render a fifth case that reads as nonsense.
 *
 * The button is the whole feature and it is deliberately unremarkable: no
 * confirmation dialog, no host picker, no "are you sure". What makes it safe
 * is not ceremony in front of it — it is that the run is staged, that each
 * host confirms before the next is touched, that a failure stops it, and that
 * a host which comes up unable to reach the Controller puts its previous agent
 * back by itself. Ceremony would suggest the danger is in the click.
 */
function Upgrade({
  offer,
  count,
  controllerVersion,
  command,
  refusal,
  onStart,
  onStop,
  onDismiss,
}: {
  offer: UpgradeOffer;
  count: number;
  controllerVersion: string | null;
  command: string | null;
  refusal: string | null;
  onStart: (version: string) => void;
  onStop: () => void;
  onDismiss: () => void;
}) {
  if (offer.kind === 'running' || offer.kind === 'ended') {
    return (
      <RolloutNotice
        offer={offer}
        onStop={onStop}
        onDismiss={onDismiss}
        refusal={refusal}
      />
    );
  }
  if (count === 0) return null;

  if (offer.kind === 'push') {
    return (
      <div className="hosts__upgrade">
        <p>
          {offer.hosts === 1 ? '1 host can' : `${offer.hosts} hosts can`} take{' '}
          {offer.version} from this Controller, one at a time.
        </p>
        <div className="actions">
          <button
            type="button"
            className="action action--primary"
            onClick={() => onStart(offer.version)}
          >
            Upgrade {offer.hosts === 1 ? 'it' : 'them'} to {offer.version}
          </button>
        </div>
        {/* The sentence that decides whether this button is frightening. Every
            claim in it is enforced somewhere other than this file. */}
        <p className="hosts__upgrade-note">
          Each host verifies the release against a key this Controller does not have, installs
          it itself, and puts the old agent back if the new one cannot reach us. One host at a
          time, and a failure stops the run.
        </p>
        {refusal ? <p className="hosts__error">{refusal}</p> : null}
        {/* The mixed fleet this lands into and stays in. A host from before
            signed push has no updater unit to trigger, so the run steps over
            it — and an operator who cannot see that reads the finished run as
            covering everything. */}
        {offer.installer > 0 && command ? (
          <>
            <p className="hosts__upgrade-note">
              {offer.installer === 1 ? '1 host is' : `${offer.installer} hosts are`} running an
              agent from before this existed, so a rollout will skip{' '}
              {offer.installer === 1 ? 'it' : 'them'}. Run this on{' '}
              {offer.installer === 1 ? 'it' : 'each of them'} once, as root, and the upgrade
              after that is this button:
            </p>
            <CopyableCommand command={command} label="Upgrade command" />
          </>
        ) : null}
      </div>
    );
  }

  // Nothing signed to push, which is the state every Controller starts in and
  // most stay in. The instruction that was here before ADR-0017, unchanged.
  return command ? (
    <UpgradeNotice count={count} controllerVersion={controllerVersion} command={command} />
  ) : null;
}

/**
 * A run, while it happens and after it stops.
 *
 * There is no progress bar, and the absence is the design. A rollout reports
 * which host it is on and a row per host once that host is done; the *fleet's*
 * progress is the version chip on each card below, which was already there. A
 * percentage here would be a second model of the same fact, and the two would
 * disagree exactly when a run went wrong.
 */
function RolloutNotice({
  offer,
  refusal,
  onStop,
  onDismiss,
}: {
  offer: Extract<UpgradeOffer, { kind: 'running' | 'ended' }>;
  refusal: string | null;
  onStop: () => void;
  onDismiss: () => void;
}) {
  const { rollout } = offer;
  const failed = rolloutFailed(rollout);
  const rows = rolloutRows(rollout);

  return (
    <div className={`hosts__upgrade${failed ? ' hosts__upgrade--failed' : ''}`}>
      <p>{describeRollout(rollout)}</p>

      {rows.length > 0 ? (
        <ul className="hosts__rollout">
          {rows.map((row) => (
            <li key={row.engineId} className={`hosts__rollout-row is-${row.state.replace(' ', '-')}`}>
              <span className="hosts__rollout-host" title={row.engineId}>
                {row.label}
              </span>
              <span className="hosts__rollout-state">{row.state}</span>
              {row.reason ? <span className="hosts__rollout-why">{row.reason}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {refusal ? <p className="hosts__error">{refusal}</p> : null}

      <div className="actions">
        {offer.kind === 'running' ? (
          <button type="button" className="action action--disruptive" onClick={onStop}>
            Stop after this host
          </button>
        ) : (
          <button type="button" className="action action--safe" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
      {offer.kind === 'running' ? (
        /* Said because the button says "after this host" and an operator
           under pressure will read that as a delay rather than as a promise.
           A host mid-transfer has written a partial file and nothing else. */
        <p className="hosts__upgrade-note">
          The host being upgraded finishes; nothing further is touched. A transfer that is
          interrupted installs nothing.
        </p>
      ) : null}
    </div>
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
  return (
    <div className="hosts__upgrade">
      <p>
        {count === 1 ? '1 host is' : `${count} hosts are`} not on this Controller's
        version, which is {controllerVersion}. Run this on each of them, as root:
      </p>
      <CopyableCommand command={command} label="Upgrade command" />
      {/* The sentence that saves an afternoon. Every other command in this
          panel carries a token, so its absence here looks like something the
          UI forgot rather than the thing that makes this an upgrade. */}
      <p className="hosts__upgrade-note">
        No token, deliberately: that is what keeps the host's identity instead of
        enrolling it again as a stranger. Nothing is deleted, and the host drops off the
        map for a second while the agent restarts.
      </p>
    </div>
  );
}

/**
 * A command to run somewhere else, with the one affordance that matters.
 *
 * Extracted because two different situations now print one: a fleet with
 * nothing to push, and the hosts a rollout will skip. It was already the
 * `token__command` field rather than a `<code>` block for a reason worth
 * keeping — that field is selectable and read-only, so an operator whose
 * browser refuses the clipboard (an insecure origin, which is exactly what a
 * Controller on a private IP is) can still select the text.
 */
function CopyableCommand({ command, label }: { command: string; label: string }) {
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
    <>
      <div className="token">
        <input
          className="token__command"
          readOnly
          value={command}
          aria-label={label}
          onFocus={(event) => event.currentTarget.select()}
        />
        <button type="button" className="control token__copy" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      {failed ? (
        <p className="hosts__upgrade-note">
          The browser would not write to the clipboard. Select the command above and copy
          it by hand.
        </p>
      ) : null}
    </>
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
