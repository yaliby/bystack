/**
 * Choosing what one host should show on the map.
 *
 * The panel is deliberately two things that look like one: a list of what has
 * been *asked for*, and the state of each as the host most recently reported
 * it. Keeping them visibly joined is what makes "stored, not yet observed" a
 * state an operator can see rather than a silence they have to interpret.
 */

import { useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import type {
  EnrolledAgent,
  GraphNode,
  InventoryItem,
  Urn,
  WatchFanout,
  WatchKind,
} from '../../../api/types';
import type { Draft, Watching } from '../model/useWatch';
import { useInventory } from '../model/useWatch';
import {
  draftFrom,
  entrySubtitle,
  entryTitle,
  fanoutCandidates,
  fanoutSummary,
  groupNote,
  inventoryNotice,
  watchRows,
} from '../model/watch';

interface Props {
  readonly baseUrl: string;
  readonly engineId: string;
  /** What to call this machine. The engine id is not a name. */
  readonly hostName: string;
  readonly watching: Watching;
  readonly nodes: ReadonlyMap<Urn, GraphNode>;
  /**
   * The fleet, so one selection can be made on more than one machine.
   *
   * The panel stays per host — this list is offered inside the picker and
   * nowhere else, because "watch nginx here" and "watch nginx on these nine"
   * are the same decision made once, not a second mode the panel is in.
   */
  readonly hosts: readonly EnrolledAgent[];
  readonly resolveHostName: (engineId: string) => string | null;
  /**
   * Something in a watch list changed.
   *
   * The fleet-wide group index is derived from these lists and nothing else
   * changes them, so this is what keeps it true without a poll.
   */
  readonly onChanged: () => void;
  /** A row is a way back to the node, which is what keeps this part of the map. */
  readonly onSelect: (urn: Urn) => void;
  readonly onClose: () => void;
}

export function WatchPanel({
  baseUrl,
  engineId,
  hostName,
  watching,
  nodes,
  hosts,
  resolveHostName,
  onChanged,
  onSelect,
  onClose,
}: Props) {
  const [picking, setPicking] = useState<WatchKind | null>(null);
  const rows = watchRows(watching.entries, nodes);

  return (
    <aside className="hosts">
      <header className="hosts__head">
        <h2>Watching on {hostName}</h2>
        <button type="button" className="hosts__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      <div className="watch__add">
        <button
          type="button"
          className="action action--safe"
          onClick={() => setPicking('unit')}
        >
          Add systemd unit
        </button>
        <button
          type="button"
          className="action action--safe"
          onClick={() => setPicking('process')}
        >
          Add process
        </button>
      </div>

      {/* Not an error. The list is durable and the host will be told when it
          comes back; saying so is what stops an operator re-adding an entry
          that is already stored. */}
      {watching.loaded && !watching.delivered && watching.detail ? (
        <p className="hosts__blocked">{watching.detail}</p>
      ) : null}

      {watching.error ? (
        <p className="hosts__error">
          {watching.error}
          <button
            type="button"
            className="actions__dismiss"
            onClick={watching.dismissError}
            aria-label="Dismiss"
          >
            ×
          </button>
        </p>
      ) : null}

      <div className="hosts__list">
        {rows.map((row) => (
          <article key={row.entry.id} className="watch__row">
            <button
              type="button"
              className="watch__target"
              // Disabled rather than hidden while pending: the entry exists,
              // and there is simply no node to navigate to yet.
              disabled={row.node === null}
              onClick={() => row.node && onSelect(row.entry.urn)}
            >
              <strong>{entryTitle(row.entry)}</strong>
              <span className="watch__sub">{entrySubtitle(row.entry)}</span>
              {/* Its own line rather than appended to the subtitle, which
                  truncates. Shown only when the entry has siblings: the entry
                  is still this host's and stopping it stops nothing elsewhere,
                  so this says what was asked for, not what the button reaches. */}
              {groupNote(row.entry) ? (
                <span className="watch__group">{groupNote(row.entry)}</span>
              ) : null}
            </button>
            <span className={`watch__state watch__state--${row.pending ? 'pending' : 'known'}`}>
              {row.state}
            </span>
            <button
              type="button"
              className="action"
              disabled={watching.busy}
              // "Stop watching", never "remove": nothing on the host changes.
              // The service goes on running exactly as it was.
              onClick={() => void watching.remove(row.entry.id).then(onChanged)}
            >
              Stop watching
            </button>
          </article>
        ))}

        {rows.length === 0 && watching.loaded ? (
          <p className="hosts__empty">
            Nothing is being watched on this host. Add a systemd unit or a process and it
            will appear on the map beside its containers.
          </p>
        ) : null}
      </div>

      {picking ? (
        <PickerDialog
          baseUrl={baseUrl}
          engineId={engineId}
          hostName={hostName}
          kind={picking}
          watching={watching}
          hosts={hosts}
          resolveHostName={resolveHostName}
          onChanged={onChanged}
          onClose={() => setPicking(null)}
        />
      ) : null}
    </aside>
  );
}

/**
 * The picker: what this machine has, asked for when it is opened.
 *
 * The free-text box below the list is not a fallback for a failed search. It
 * is the answer to "the thing I want is not installed yet" — a unit an
 * operator is about to deploy, or a daemon that is currently down — and it is
 * the only way to watch something that does not exist at the moment of
 * watching it.
 */
function PickerDialog({
  baseUrl,
  engineId,
  hostName,
  kind,
  watching,
  hosts,
  resolveHostName,
  onChanged,
  onClose,
}: {
  readonly baseUrl: string;
  readonly engineId: string;
  readonly hostName: string;
  readonly kind: WatchKind;
  readonly watching: Watching;
  readonly hosts: readonly EnrolledAgent[];
  readonly resolveHostName: (engineId: string) => string | null;
  readonly onChanged: () => void;
  readonly onClose: () => void;
}) {
  const [filter, setFilter] = useState('');
  const [manual, setManual] = useState('');
  const [label, setLabel] = useState('');
  // The other machines this selection should also be made on. Empty is the
  // normal case and the default: a picker opened from one host's panel is
  // about that host until somebody says otherwise.
  const [also, setAlso] = useState<readonly string[]>([]);
  // Held rather than dismissed on success, when there is anything to say. A
  // fan-out that half worked closing itself would take the only account of
  // what happened with it.
  const [outcome, setOutcome] = useState<WatchFanout | null>(null);
  const { inventory, loading } = useInventory(baseUrl, engineId, kind, filter, true);
  const notice = inventoryNotice(inventory);

  const candidates = useMemo(
    () => fanoutCandidates(hosts, engineId, resolveHostName),
    [hosts, engineId, resolveHostName],
  );
  const labelFor = (id: string) =>
    id === engineId ? hostName : (candidates.find((host) => host.engineId === id)?.label ?? id);

  /**
   * One place both entry points end up, because the only difference between
   * picking a row and typing a name is how the draft was built.
   *
   * The host being edited leads the list, so the results read in the order
   * the operator was thinking: this machine, then the ones they added.
   */
  const submit = async (draft: Draft) => {
    if (also.length === 0) {
      const ok = await watching.add(draft);
      if (ok) {
        onChanged();
        onClose();
      }
      return;
    }
    const result = await watching.addAcross(draft, [engineId, ...also]);
    if (result === null) return;
    onChanged();
    if (result.stored === result.hosts.length && result.hosts.every((h) => h.detail === null)) {
      onClose();
      return;
    }
    setOutcome(result);
  };

  const choose = (item: InventoryItem) => submit({ ...draftFrom(kind, item), label });

  const chooseManually = () => {
    if (!manual.trim()) return Promise.resolve();
    return submit(
      kind === 'unit'
        ? { kind, name: manual.trim(), match_kind: null, pattern: '', label }
        : {
            kind,
            name: '',
            // A typed pattern is matched against the command line unless it is
            // plainly a path. That is the choice that makes `worker.py` do
            // what somebody typing it expects.
            match_kind: manual.trim().startsWith('/') ? 'exec' : 'cmdline',
            pattern: manual.trim(),
            label,
          },
    );
  };

  // Portalled for the same reason as the join-token dialog: this is rendered
  // from inside a panel that has a `backdrop-filter`, which makes that panel
  // the containing block for `position: fixed` and would lay the picker out
  // inside a 340px column.
  return createPortal(
    <div className="modal">
      <div className="modal__card">
        <header className="hosts__head">
          <h2>{kind === 'unit' ? 'Add a systemd unit' : 'Add a process'}</h2>
          <button type="button" className="hosts__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <input
          className="watch__filter"
          placeholder={kind === 'unit' ? 'Filter units…' : 'Filter processes…'}
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />

        {notice ? <p className="hosts__blocked">{notice}</p> : null}

        <div className="watch__results">
          {loading && inventory === null ? <p className="hosts__empty">Asking the host…</p> : null}
          {(inventory?.items ?? []).map((item) => (
            <button
              key={`${item.id}:${item.pid}`}
              type="button"
              className="watch__result"
              disabled={watching.busy}
              onClick={() => void choose(item)}
            >
              <strong>{item.name || item.id}</strong>
              <span className="watch__sub">
                {item.state}
                {item.detail ? ` · ${item.detail}` : ''}
              </span>
            </button>
          ))}
          {inventory?.ok && inventory.items.length === 0 ? (
            <p className="hosts__empty">
              Nothing here matches. It may not be installed — add it by name below and the
              map will say so until it appears.
            </p>
          ) : null}
        </div>

        <label className="watch__manual">
          <span>{kind === 'unit' ? 'Or a unit name' : 'Or a command line to match'}</span>
          <input
            value={manual}
            placeholder={kind === 'unit' ? 'nginx' : 'worker.py'}
            onChange={(event) => setManual(event.target.value)}
          />
        </label>

        <label className="watch__manual">
          <span>Call it (optional)</span>
          <input value={label} onChange={(event) => setLabel(event.target.value)} />
        </label>

        {candidates.length > 0 ? (
          <fieldset className="watch__also">
            {/* "Also on", never "apply to". Each host gets its own ordinary
                entry that it then owns: nothing here creates a rule that keeps
                acting on machines after this dialog closes. */}
            <legend>
              Also watch it on
              <button
                type="button"
                className="watch__all"
                onClick={() =>
                  setAlso((current) =>
                    current.length === candidates.length
                      ? []
                      : candidates.map((host) => host.engineId),
                  )
                }
              >
                {also.length === candidates.length ? 'None' : `All ${candidates.length}`}
              </button>
            </legend>
            <div className="watch__hosts">
              {candidates.map((host) => (
                <label key={host.engineId} className="watch__host">
                  <input
                    type="checkbox"
                    checked={also.includes(host.engineId)}
                    onChange={(event) =>
                      setAlso((current) =>
                        event.target.checked
                          ? [...current, host.engineId]
                          : current.filter((id) => id !== host.engineId),
                      )
                    }
                  />
                  <span>{host.label}</span>
                </label>
              ))}
            </div>
            {also.length > 0 ? (
              // Said plainly, because the two are not the same thing and the
              // difference is the whole feature: separate entries, one origin.
              <p className="watch__sub">
                {also.length + 1} separate entries, one on each host, recorded as chosen
                together.
              </p>
            ) : null}
          </fieldset>
        ) : null}

        {outcome ? <FanoutReport result={outcome} labelFor={labelFor} onClose={onClose} /> : null}

        <button
          type="button"
          className="action action--safe"
          disabled={watching.busy || !manual.trim()}
          onClick={() => void chooseManually()}
        >
          {also.length > 0 ? `Watch it on ${also.length + 1} hosts` : 'Watch it'}
        </button>
      </div>
    </div>,
    document.body,
  );
}

/**
 * What a fan-out did, once it has done it.
 *
 * Shown only when there is something to look at — a clean run closes the
 * dialog instead, because a report saying "all nine worked" is a second click
 * charged for nothing. The hosts that need attention are named, never
 * counted: "stored on 8 of 9" sends an operator to find the ninth, and this
 * already knows which one it is.
 */
function FanoutReport({
  result,
  labelFor,
  onClose,
}: {
  readonly result: WatchFanout;
  readonly labelFor: (engineId: string) => string;
  readonly onClose: () => void;
}) {
  const { headline, problems } = fanoutSummary(result, labelFor);
  return (
    <div className="watch__report">
      <p className="hosts__blocked">{headline}</p>
      {problems.map((problem) => (
        <p key={problem} className="watch__sub">
          {problem}
        </p>
      ))}
      <button type="button" className="action" onClick={onClose}>
        Done
      </button>
    </div>
  );
}
