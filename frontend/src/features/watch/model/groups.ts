/**
 * Watch groups, seen from the operations side.
 *
 * The panel in `watch.ts` asks "what did I select on this host". This file
 * asks the other question — *this card is one of nine; which machines are the
 * other eight and what are they called* — which no single host's list can
 * answer, because the other eight live in other partitions.
 *
 * Pure: no React, no network.
 *
 * **A group is a set of independent entries and this file does not pretend
 * otherwise.** It resolves membership so an operator can choose a scope; it
 * never decides one. Nothing here implies that acting on one member should
 * act on the rest — that is a choice made at the moment of pressing the
 * button, which is exactly what the operator asked to keep.
 */

import type { GroupCommandResult, Urn, WatchEntry } from '../../../api/types';
import { hostLabel } from '../../hosts/model/hosts';

/** One host's copy of a grouped selection. */
export interface GroupMember {
  readonly engineId: string;
  /** The node on that host. What a command would be aimed at. */
  readonly urn: Urn;
  /** What to call the machine. The engine id is not a name. */
  readonly label: string;
}

export interface WatchGroup {
  readonly groupId: string;
  /** Every host holding an entry from this selection, by label. */
  readonly members: readonly GroupMember[];
}

/**
 * Every watched node in the fleet, pointed at the group it belongs to.
 *
 * Keyed by URN because that is what the canvas selects and what a command
 * targets — so the operations bar can ask one question of one map, without
 * knowing that watch entries exist at all.
 *
 * Groups of one are included. The caller decides whether one member is worth
 * offering a scope for; leaving them out here would make "is this watched?"
 * and "is this grouped?" the same question, and they are not.
 */
export function indexGroups(
  entries: readonly WatchEntry[],
  resolveHostName: (engineId: string) => string | null,
): ReadonlyMap<Urn, WatchGroup> {
  const byGroup = new Map<string, GroupMember[]>();
  for (const entry of entries) {
    const members = byGroup.get(entry.group_id) ?? [];
    members.push({
      engineId: entry.engine_id,
      urn: entry.urn,
      label: hostLabel(entry.engine_id, resolveHostName(entry.engine_id)),
    });
    byGroup.set(entry.group_id, members);
  }

  const index = new Map<Urn, WatchGroup>();
  for (const [groupId, members] of byGroup) {
    // Sorted by label so the host list reads the same on every render and in
    // every panel. The order entries were added in is an implementation
    // detail of the fan-out, not something an operator can reason about.
    const sorted = [...members].sort((left, right) => left.label.localeCompare(right.label));
    const group: WatchGroup = { groupId, members: sorted };
    for (const member of sorted) index.set(member.urn, group);
  }
  return index;
}

/**
 * What a group command did, in words.
 *
 * Shaped like `fanoutSummary` on purpose: the two are the same sentence about
 * different verbs, and an operator who has read one should not have to learn
 * the other. Hosts that need attention are named rather than counted.
 */
export function groupCommandSummary(result: GroupCommandResult): {
  readonly headline: string;
  readonly problems: readonly string[];
  readonly ok: boolean;
} {
  const total = result.hosts.length;
  const ok = result.status === 'succeeded' || result.status === 'noop';
  const acted = result.hosts.filter((host) => host.ran).length;

  return {
    ok,
    headline: ok
      ? `Done on ${total === 1 ? '1 host' : `${total} hosts`}.`
      : `${acted} of ${total} hosts acted on.`,
    // A host that ran and failed is as much of a problem as one that never
    // ran, and the two have to sit in the same list: both end with a machine
    // that is not in the state the operator just asked for.
    problems: result.hosts
      .filter((host) => !host.ran || (host.status !== 'succeeded' && host.status !== 'noop'))
      .map((host) => `${host.engine_id}: ${host.detail ?? host.status}`),
  };
}

/**
 * The hosts a chosen scope resolves to.
 *
 * Three modes and one mechanism, which is the point: "this host", "these
 * four" and "all nine" differ only in the list, so there is nothing for the
 * Controller to interpret and no way for `all` to mean something different by
 * the time the request lands than it did on the screen.
 */
export type Scope = { readonly mode: 'one' } | { readonly mode: 'some'; readonly engineIds: readonly string[] } | { readonly mode: 'all' };

export function scopeHosts(group: WatchGroup, scope: Scope, hereEngineId: string): readonly string[] {
  switch (scope.mode) {
    case 'one':
      return [hereEngineId];
    case 'all':
      return group.members.map((member) => member.engineId);
    case 'some':
      // The host whose card is open is always in it. It is the subject of the
      // sentence — the operator opened this node, not a fleet view — and a
      // scope that silently excluded it would act on everything except the
      // thing being looked at.
      return [hereEngineId, ...scope.engineIds.filter((id) => id !== hereEngineId)];
  }
}

/**
 * Cards the canvas should light for a chosen scope.
 *
 * Empty while the operator is still on "this host" — that is ordinary
 * selection, and the trace already answers it. Choose / All are the modes
 * where the press reaches nodes the operator is *not* looking at, so those
 * exact target cards have to announce themselves on the map before the
 * button is pressed. Hosts and anything else only linked to them stay out:
 * the command aims at the unit or process, not at its neighbours.
 */
export function scopeMarks(group: WatchGroup, scope: Scope, hereEngineId: string): readonly Urn[] {
  if (scope.mode === 'one') return [];
  const reached = new Set(scopeHosts(group, scope, hereEngineId));
  return group.members
    .filter((member) => reached.has(member.engineId))
    .map((member) => member.urn);
}

/** How a scope reads on the button, so the operator knows what they are about to do. */
export function scopeLabel(count: number): string {
  return count === 1 ? 'this host' : `${count} hosts`;
}
