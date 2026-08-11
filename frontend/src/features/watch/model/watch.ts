/**
 * What the watch panel is allowed to say about a selection.
 *
 * Pure: no React, no network. Two of the three decisions here are ones a panel
 * gets wrong by default, which is why they live where they can be tested
 * without a browser.
 *
 * **A stored entry and a drawn card are different things.** The list is the
 * Controller's and it is durable; the card comes from the host and appears
 * when the host says something. Between those two is a real state — "stored,
 * not yet observed" — and a panel that renders the list alone reads as a
 * feature that silently did nothing, while one that renders the graph alone
 * loses the entry an operator just added to a machine that is asleep.
 *
 * **An empty answer is not a refusal.** `Inventory.ok` separates "this machine
 * has no unit matching what you typed" from "the agent could not be asked",
 * and both are an empty list.
 */

import type {
  GraphNode,
  Inventory,
  InventoryItem,
  MatchKind,
  Urn,
  WatchEntry,
  WatchKind,
} from '../../../api/types';

/** A row in the panel: what was asked for, beside what the host said. */
export interface WatchRow {
  readonly entry: WatchEntry;
  /** The node, once the host has reported it. */
  readonly node: GraphNode | null;
  /** What to show as the state, in words. */
  readonly state: string;
  /**
   * Whether this entry is waiting on the host rather than reporting anything.
   *
   * True only before the first observation. A watch on a unit that is *not
   * installed* is emphatically not pending: the host answered, and the answer
   * was `not-found`.
   */
  readonly pending: boolean;
}

/** How an entry reads when there is nothing else to call it. */
export function entryTitle(entry: WatchEntry): string {
  if (entry.label) return entry.label;
  if (entry.kind === 'unit') return entry.name;
  // The operator's own pattern, shortened to its last path segment for an
  // exec rule: `/usr/local/bin/mydaemon` reads as `mydaemon`.
  const trimmed = entry.pattern.replace(/\/+$/, '');
  return trimmed.slice(trimmed.lastIndexOf('/') + 1) || trimmed;
}

/** The second line: how this entry finds what it is watching. */
export function entrySubtitle(entry: WatchEntry): string {
  if (entry.kind === 'unit') return 'systemd unit';
  switch (entry.match_kind) {
    case 'exec':
      return `executable ${entry.pattern}`;
    case 'name':
      return `named ${entry.pattern}`;
    default:
      return `command line contains ${entry.pattern}`;
  }
}

/**
 * The words a state gets in this panel.
 *
 * Deliberately not the raw value. `not-found` and `absent` are the two an
 * operator misreads most: the first is not "stopped" and the second is not
 * "broken", and both are the normal answer for something that is simply not
 * there right now.
 */
export function stateLabel(node: GraphNode | null): string {
  if (node === null) return 'Waiting for this host';
  switch (node.status) {
    case 'active':
      return 'Running';
    case 'inactive':
      return 'Stopped';
    case 'failed':
      return 'Failed';
    case 'activating':
      return 'Starting';
    case 'deactivating':
      return 'Stopping';
    case 'not-found':
      return 'Not installed';
    case 'masked':
      return 'Masked';
    case 'error':
      return 'Cannot be read';
    case 'running':
      return 'Running';
    case 'absent':
      return 'Not running';
    case 'zombie':
      return 'Zombie';
    case 'stopped':
      return 'Suspended';
    default:
      return node.status ?? 'Unknown';
  }
}

/**
 * Join the stored list to what the graph currently holds.
 *
 * By URN, which is the whole reason `WatchEntryOut.urn` exists: the Controller
 * computes the identity a stored entry *will* have, so the panel can look for
 * the node without knowing how a unit name becomes a URN. A second copy of
 * that rule in TypeScript would be correct the day it was written.
 */
export function watchRows(
  entries: readonly WatchEntry[],
  nodes: ReadonlyMap<Urn, GraphNode>,
): readonly WatchRow[] {
  return entries.map((entry) => {
    const node = nodes.get(entry.urn) ?? null;
    return {
      entry,
      node,
      state: stateLabel(node),
      pending: node === null,
    };
  });
}

/**
 * What a picker row would become if it were selected.
 *
 * The one place the two kinds diverge, and it is the identity question again:
 * a unit is named by systemd and a process is named by a rule, so picking a
 * process row means choosing *how* it will be matched from then on. `exec`
 * where the agent could resolve an executable path, because that is the
 * precise choice; `name` otherwise, which is all the kernel will tell us about
 * a process this agent does not own.
 */
export function draftFrom(
  kind: WatchKind,
  item: InventoryItem,
): { readonly kind: WatchKind; readonly name: string; readonly match_kind: MatchKind | null; readonly pattern: string } {
  if (kind === 'unit') {
    return { kind, name: item.id, match_kind: null, pattern: '' };
  }
  const isPath = item.id.startsWith('/');
  return {
    kind,
    name: '',
    match_kind: isPath ? 'exec' : 'name',
    pattern: item.id,
  };
}

/**
 * What to say above the picker's rows.
 *
 * `null` when the list speaks for itself. The two cases that need words are
 * the refusal — which is not an empty machine — and the cap, because an
 * operator whose service is number three hundred would otherwise conclude it
 * is not installed.
 */
export function inventoryNotice(inventory: Inventory | null): string | null {
  if (inventory === null) return null;
  if (!inventory.ok) return inventory.reason ?? 'This host could not be asked.';
  if (inventory.total > inventory.items.length) {
    return `Showing ${inventory.items.length} of ${inventory.total}. Type to narrow it.`;
  }
  return null;
}
