/**
 * Choosing what one host should show on the map.
 *
 * The panel is deliberately two things that look like one: a list of what has
 * been *asked for*, and the state of each as the host most recently reported
 * it. Keeping them visibly joined is what makes "stored, not yet observed" a
 * state an operator can see rather than a silence they have to interpret.
 */

import { useState } from 'react';
import { createPortal } from 'react-dom';
import type { GraphNode, InventoryItem, Urn, WatchKind } from '../../../api/types';
import type { Watching } from '../model/useWatch';
import { useInventory } from '../model/useWatch';
import {
  draftFrom,
  entrySubtitle,
  entryTitle,
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
          Add service
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
              onClick={() => void watching.remove(row.entry.id)}
            >
              Stop watching
            </button>
          </article>
        ))}

        {rows.length === 0 && watching.loaded ? (
          <p className="hosts__empty">
            Nothing is being watched on this host. Add a service or a process and it will
            appear on the map beside its containers.
          </p>
        ) : null}
      </div>

      {picking ? (
        <PickerDialog
          baseUrl={baseUrl}
          engineId={engineId}
          kind={picking}
          watching={watching}
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
  kind,
  watching,
  onClose,
}: {
  readonly baseUrl: string;
  readonly engineId: string;
  readonly kind: WatchKind;
  readonly watching: Watching;
  readonly onClose: () => void;
}) {
  const [filter, setFilter] = useState('');
  const [manual, setManual] = useState('');
  const [label, setLabel] = useState('');
  const { inventory, loading } = useInventory(baseUrl, engineId, kind, filter, true);
  const notice = inventoryNotice(inventory);

  const choose = async (item: InventoryItem) => {
    const ok = await watching.add({ ...draftFrom(kind, item), label });
    if (ok) onClose();
  };

  const chooseManually = async () => {
    if (!manual.trim()) return;
    const draft =
      kind === 'unit'
        ? { kind, name: manual.trim(), match_kind: null, pattern: '', label }
        : {
            kind,
            name: '',
            // A typed pattern is matched against the command line unless it is
            // plainly a path. That is the choice that makes `worker.py` do
            // what somebody typing it expects.
            match_kind: (manual.trim().startsWith('/') ? 'exec' : 'cmdline') as 'exec' | 'cmdline',
            pattern: manual.trim(),
            label,
          };
    const ok = await watching.add(draft);
    if (ok) onClose();
  };

  // Portalled for the same reason as the join-token dialog: this is rendered
  // from inside a panel that has a `backdrop-filter`, which makes that panel
  // the containing block for `position: fixed` and would lay the picker out
  // inside a 340px column.
  return createPortal(
    <div className="modal">
      <div className="modal__card">
        <header className="hosts__head">
          <h2>{kind === 'unit' ? 'Add a service' : 'Add a process'}</h2>
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

        <button
          type="button"
          className="action action--safe"
          disabled={watching.busy || !manual.trim()}
          onClick={() => void chooseManually()}
        >
          Watch it
        </button>
      </div>
    </div>,
    document.body,
  );
}
