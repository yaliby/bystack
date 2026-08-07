/**
 * Operation controls for the selected node.
 *
 * Presentation only. Which actions exist comes from the Controller, what a
 * result means comes from `operations.ts`, and this file renders both.
 */

import type { Actions, CommandKind, Urn } from '../../../api/types';
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
}

export function ActionBar({
  actions,
  phase,
  busy,
  name,
  resolveName,
  onRun,
  onDismiss,
}: Props) {
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

  return (
    <section className="inspector__section">
      <h3>
        Operations
        {actions.targets.length > 1 ? (
          <span className="actions__scope">{actions.targets.length} containers</span>
        ) : null}
      </h3>

      {blocked ? (
        <p className="actions__blocked">{explanation}</p>
      ) : (
        <div className="actions">
          {actions.commands.map((kind) => (
            <button
              key={kind}
              type="button"
              className={`action action--${tone(kind)}`}
              disabled={busy}
              onClick={() => {
                // `confirm` rather than a modal: this is a single yes/no on a
                // decision the operator just made deliberately, and a custom
                // dialog here would be more code and less accessible for it.
                if (
                  needsConfirmation(kind, actions) &&
                  !window.confirm(confirmationText(kind, actions, name))
                ) {
                  return;
                }
                onRun(kind);
              }}
            >
              {ACTION_LABEL[kind]}
            </button>
          ))}
        </div>
      )}

      {phase ? <PhaseLine phase={phase} resolveName={resolveName} onDismiss={onDismiss} /> : null}
    </section>
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
