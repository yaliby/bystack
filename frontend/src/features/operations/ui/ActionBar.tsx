/**
 * Operation controls for the selected node.
 *
 * Presentation only. Which actions exist comes from the Controller, what a
 * result means comes from `operations.ts`, and this file renders both.
 */

import { useEffect, useState } from 'react';
import type { Actions, CommandKind, GroupCommandResult, Urn } from '../../../api/types';
import {
  groupCommandSummary,
  scopeHosts,
  scopeLabel,
  scopeMarks,
  type Scope,
  type WatchGroup,
} from '../../watch/model/groups';
import {
  ACTION_LABEL,
  DESTRUCTIVE,
  DISRUPTIVE,
  OPERABLE_KINDS,
  REASON_TEXT,
  confirmationText,
  needsConfirmation,
  summarize,
  toneOf,
  type Phase,
} from '../model/operations';

interface Props {
  readonly actions: Actions | null;
  readonly phase: Phase | null;
  readonly busy: boolean;
  readonly name: string;
  readonly resolveName: (urn: Urn) => string;
  readonly onRun: (kind: CommandKind) => void;
  readonly onDismiss: () => void;

  /**
   * The selection this node came from, when it came from one made on several
   * hosts at once.
   *
   * Optional and usually absent: a container has no watch group, and a
   * service chosen for one machine is a group of one. When it *is* here, the
   * bar offers a scope — and offering it is the entire feature, because the
   * group carries no authority of its own. Nothing acts on nine hosts unless
   * somebody says so at the moment of pressing the button.
   */
  readonly group?: WatchGroup | null;
  readonly groupBusy?: boolean;
  readonly groupResult?: GroupCommandResult | null;
  readonly groupError?: string | null;
  readonly onRunGroup?: (kind: CommandKind, engineIds: readonly string[]) => void;
  readonly onDismissGroup?: () => void;
  /**
   * Cards the canvas should light for the current scope. Empty while the
   * operator is on "this host"; Choose / All report every target the next
   * press would reach — the unit or process itself, not its linked host.
   */
  readonly onAffecting?: (urns: readonly Urn[]) => void;
}

export function ActionBar({
  actions,
  phase,
  busy,
  name,
  resolveName,
  onRun,
  onDismiss,
  group = null,
  groupBusy = false,
  groupResult = null,
  groupError = null,
  onRunGroup,
  onDismissGroup,
  onAffecting,
}: Props) {
  // Reset to "this host" whenever the selection moves. A scope is a statement
  // about the thing in front of the operator, and carrying one across nodes is
  // how somebody restarts nine machines meaning to restart one.
  const [scope, setScope] = useState<Scope>({ mode: 'one' });
  const [scopeFor, setScopeFor] = useState<string>('');
  const anchor = group?.groupId ?? '';
  if (anchor !== scopeFor) {
    setScopeFor(anchor);
    setScope({ mode: 'one' });
  }

  // Tell the canvas which cards the next press reaches. Cleared on unmount
  // and whenever the scope collapses back to one host, so a leftover mark
  // cannot outlive the picker that owned it.
  const scopedPreview = group && group.members.length > 1 && onRunGroup ? group : null;
  const herePreview =
    scopedPreview?.members.find((member) => member.urn === actions?.target)?.engineId ?? '';
  useEffect(() => {
    if (!onAffecting) return;
    if (!scopedPreview || !herePreview) {
      onAffecting([]);
      return;
    }
    onAffecting(scopeMarks(scopedPreview, scope, herePreview));
    return () => onAffecting([]);
  }, [onAffecting, scopedPreview, herePreview, scope]);

  if (!actions) return null;

  // A host, network, volume or image has no lifecycle to operate, and saying
  // so on every selection is noise rather than information. The section only
  // exists for things that could have actions.
  if (!OPERABLE_KINDS.has(actions.kind)) return null;

  const blocked = actions.commands.length === 0;
  const explanation = actions.reason ? REASON_TEXT[actions.reason] : null;

  // Nothing to offer and nothing to explain: render nothing rather than an
  // empty labelled section, which reads as a feature that failed to load.
  if (blocked && !explanation) return null;

  // A group of one is not a scope. The machine whose card is open is the only
  // member, so offering to choose between it and itself is a control that
  // cannot change anything.
  const scoped = scopedPreview;
  const here = herePreview;
  const hosts = scoped && here ? scopeHosts(scoped, scope, here) : [];

  return (
    <section className="inspector__section">
      <h3>
        Operations
        {actions.targets.length > 1 ? (
          <span className="actions__scope">{actions.targets.length} containers</span>
        ) : null}
      </h3>

      {scoped && here ? (
        <ScopePicker
          group={scoped}
          scope={scope}
          here={here}
          onChange={setScope}
          disabled={busy || groupBusy}
        />
      ) : null}

      {blocked ? (
        <p className="actions__blocked">{explanation}</p>
      ) : (
        <div className="actions">
          {actions.commands.map((kind) => (
            <button
              key={kind}
              type="button"
              className={`action action--${tone(kind)}`}
              disabled={busy || groupBusy}
              onClick={() => {
                // `confirm` rather than a modal: this is a single yes/no on a
                // decision the operator just made deliberately, and a custom
                // dialog here would be more code and less accessible for it.
                //
                // Beyond one host it is asked *unconditionally*, whatever the
                // verb. "Restart nginx" and "restart nginx on nine machines"
                // are different decisions, and the second one deserves to be
                // said out loud even when the first would not have been.
                const many = hosts.length > 1;
                if (many) {
                  if (
                    !window.confirm(
                      `${ACTION_LABEL[kind]} ${name} on ${hosts.length} hosts?\n\n` +
                        `${scoped?.members
                          .filter((member) => hosts.includes(member.engineId))
                          .map((member) => member.label)
                          .join(', ')}\n\n` +
                        'Each host is acted on separately; one failing does not stop the rest.',
                    )
                  ) {
                    return;
                  }
                } else if (
                  needsConfirmation(kind, actions) &&
                  !window.confirm(confirmationText(kind, actions, name))
                ) {
                  return;
                }

                // One host goes down the ordinary path, unchanged. There is
                // no reason for a scope of one to take a different route than
                // a node with no group at all, and one code path here is one
                // fewer way for the two to disagree.
                if (many && scoped && onRunGroup) onRunGroup(kind, hosts);
                else onRun(kind);
              }}
            >
              {ACTION_LABEL[kind]}
              {hosts.length > 1 ? <span className="action__count">{hosts.length}</span> : null}
            </button>
          ))}
        </div>
      )}

      {groupError ? (
        <p className="actions__phase actions__phase--critical">
          {groupError}
          {onDismissGroup ? <Dismiss onDismiss={onDismissGroup} /> : null}
        </p>
      ) : null}

      {groupBusy ? <p className="actions__phase actions__phase--busy">Working…</p> : null}

      {groupResult && onDismissGroup ? (
        <GroupPhase result={groupResult} onDismiss={onDismissGroup} />
      ) : null}

      {phase ? <PhaseLine phase={phase} resolveName={resolveName} onDismiss={onDismiss} /> : null}
    </section>
  );
}

/**
 * Which machines the next button press reaches.
 *
 * Three choices and one mechanism: each resolves to a list of hosts, and the
 * Controller is told the list rather than the word. That is what makes "all"
 * mean *the ones I am looking at* — a server-side "all" would act on a host
 * enrolled between reading the screen and pressing the button.
 *
 * Defaults to this host on every selection. The safe direction is the narrow
 * one, and a scope that persisted would be the mechanism by which somebody
 * restarts nine machines meaning to restart one.
 */
function ScopePicker({
  group,
  scope,
  here,
  onChange,
  disabled,
}: {
  readonly group: WatchGroup;
  readonly scope: Scope;
  readonly here: string;
  readonly onChange: (scope: Scope) => void;
  readonly disabled: boolean;
}) {
  const count = scopeHosts(group, scope, here).length;
  const others = group.members.filter((member) => member.engineId !== here);
  const chosen = scope.mode === 'some' ? scope.engineIds : [];

  return (
    <div className="scope">
      <div className="scope__modes" role="group" aria-label="Which hosts to act on">
        <button
          type="button"
          className="scope__mode"
          aria-pressed={scope.mode === 'one'}
          disabled={disabled}
          onClick={() => onChange({ mode: 'one' })}
        >
          This host
        </button>
        <button
          type="button"
          className="scope__mode"
          aria-pressed={scope.mode === 'some'}
          disabled={disabled}
          onClick={() => onChange({ mode: 'some', engineIds: [] })}
        >
          Choose
        </button>
        <button
          type="button"
          className="scope__mode"
          aria-pressed={scope.mode === 'all'}
          disabled={disabled}
          onClick={() => onChange({ mode: 'all' })}
        >
          All {group.members.length}
        </button>
      </div>

      {scope.mode === 'some' ? (
        <div className="scope__hosts">
          {/* This host is shown and fixed rather than omitted: the operator
              opened *this* node, and a list that quietly excluded it would
              act on everything except the thing being looked at. */}
          <label className="scope__host scope__host--fixed">
            <input type="checkbox" checked disabled />
            <span>{group.members.find((m) => m.engineId === here)?.label ?? here}</span>
          </label>
          {others.map((member) => (
            <label key={member.engineId} className="scope__host">
              <input
                type="checkbox"
                disabled={disabled}
                checked={chosen.includes(member.engineId)}
                onChange={(event) =>
                  onChange({
                    mode: 'some',
                    engineIds: event.target.checked
                      ? [...chosen, member.engineId]
                      : chosen.filter((id) => id !== member.engineId),
                  })
                }
              />
              <span>{member.label}</span>
            </label>
          ))}
        </div>
      ) : null}

      <p className="scope__summary">Acting on {scopeLabel(count)}.</p>
    </div>
  );
}

/**
 * What a command sent to several hosts did.
 *
 * No confirmation-by-discovery here, unlike the single-host phase: that one
 * witnesses a revision on one node and waits for the delta that changes it,
 * and doing the same across nine hosts would be nine witnesses and one
 * sentence that could not describe them. What this reports is what the hosts
 * answered, which is the honest half — and the cards on the map still turn
 * over as discovery observes each machine.
 */
function GroupPhase({
  result,
  onDismiss,
}: {
  readonly result: GroupCommandResult;
  readonly onDismiss: () => void;
}) {
  const { headline, problems, ok } = groupCommandSummary(result);
  return (
    <div className={`actions__phase actions__phase--${ok ? 'good' : 'warning'}`}>
      <p>
        {headline}
        <Dismiss onDismiss={onDismiss} />
      </p>
      {problems.map((problem) => (
        <p key={problem} className="scope__problem">
          {problem}
        </p>
      ))}
    </div>
  );
}

function PhaseLine({
  phase,
  resolveName,
  onDismiss,
}: {
  phase: Phase;
  resolveName: (urn: Urn) => string;
  onDismiss: () => void;
}) {
  switch (phase.kind) {
    case 'running':
      return (
        <p className="actions__phase actions__phase--busy">
          {ACTION_LABEL[phase.command]}…
        </p>
      );

    case 'confirming':
      // The honest description of the gap between "the engine did it" and
      // "we have seen it". Not a spinner pretending the request is still in
      // flight — it finished, and this says what we are actually waiting on.
      return (
        <p className="actions__phase actions__phase--busy">
          Applied · waiting for discovery to confirm
        </p>
      );

    case 'unconfirmed':
      return (
        <p className="actions__phase actions__phase--warning">
          The engine accepted this, but the topology has not changed. The host may have
          lost its event stream.
          <Dismiss onDismiss={onDismiss} />
        </p>
      );

    case 'done':
      return (
        <p className={`actions__phase actions__phase--${toneOf(phase.result.status)}`}>
          {summarize(phase.result, resolveName)}
          <Dismiss onDismiss={onDismiss} />
        </p>
      );

    case 'error':
      return (
        <p className="actions__phase actions__phase--critical">
          {phase.message}
          <Dismiss onDismiss={onDismiss} />
        </p>
      );
  }
}

function Dismiss({ onDismiss }: { onDismiss: () => void }) {
  return (
    <button type="button" className="actions__dismiss" onClick={onDismiss} aria-label="Dismiss">
      ×
    </button>
  );
}

function tone(kind: CommandKind): string {
  if (DESTRUCTIVE.has(kind)) return 'destructive';
  if (DISRUPTIVE.has(kind)) return 'disruptive';
  return 'safe';
}
