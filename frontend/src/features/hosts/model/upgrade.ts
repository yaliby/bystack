/**
 * What the Hosts panel is allowed to say about upgrading a fleet.
 *
 * Pure: no React, no network. Everything here is a consequence of one thing
 * the backend deliberately does *not* provide, and a panel gets it wrong by
 * default: **there is no progress model.**
 *
 * A rollout reports the version it is installing, who it planned to touch,
 * which host it is on right now, and a row per host once that host is done.
 * Progress is the fleet's own state changing — the version chip on each card,
 * which was already there (ADR-0015's skew report). Inventing a percentage
 * here would mean maintaining a second model of the same fact, and the two
 * would disagree exactly when a run went wrong.
 *
 * The other thing this file exists for is that **two upgrade paths are live at
 * once**, permanently and by design. A host running an agent from before
 * ADR-0017 has no updater unit to trigger, so it is upgraded by running the
 * installer on it once; every host after that is upgraded by a button. Showing
 * one instruction to an operator whose fleet needs both is how four machines
 * get left behind with no explanation.
 */

import type { AgentReleases, EnrolledAgent, Rollout } from '../../../api/types';
import { versionSkew } from './hosts';

/**
 * What the panel should offer, given what the Controller holds and what the
 * fleet is running.
 *
 * A closed set rather than a pile of booleans, because the four cases want
 * four different paragraphs and a UI assembled from flags reliably produces a
 * fifth that reads as nonsense.
 */
export type UpgradeOffer =
  /** Nothing to say: no skew, or nothing held and nothing behind. */
  | { readonly kind: 'none' }
  /** A run is under way. The panel watches rather than offers. */
  | { readonly kind: 'running'; readonly rollout: Rollout }
  /** A run ended. Worth reporting until an operator dismisses it. */
  | { readonly kind: 'ended'; readonly rollout: Rollout }
  /**
   * Hosts are behind and this Controller can push to some of them.
   * `installer` counts the ones it cannot, which need the command instead.
   */
  | { readonly kind: 'push'; readonly version: string; readonly hosts: number; readonly installer: number }
  /** Hosts are behind and nothing here can be pushed. The old path, unchanged. */
  | { readonly kind: 'installer'; readonly hosts: number };

/**
 * Which of those applies.
 *
 * Order matters and is not arbitrary. A live run outranks everything, because
 * an operator looking at this panel during one is asking about *it*, not about
 * whether they should start another. A finished run outranks the offer for the
 * same reason it outranks silence: a rollout that stopped on a host is the
 * single most important sentence on this screen, and re-offering the button
 * above it would read as if nothing had happened.
 */
export function upgradeOffer(
  agents: readonly EnrolledAgent[],
  controllerVersion: string | null,
  releases: AgentReleases | null,
  rollout: Rollout | null,
): UpgradeOffer {
  if (rollout && rollout.state === 'running') return { kind: 'running', rollout };
  if (rollout) return { kind: 'ended', rollout };

  const behind = agents.filter((agent) => versionSkew(agent, controllerVersion));
  if (behind.length === 0) return { kind: 'none' };

  const pushable = releases?.upgradable ?? [];
  const target = releases?.releases[0]?.version;
  // A release must exist *and* somebody must be able to take it. Either alone
  // is the installer's case: a Controller holding artifacts nobody can verify
  // is not a Controller that can push.
  if (target && pushable.length > 0) {
    const byId = new Set(pushable);
    const hosts = behind.filter((agent) => byId.has(agent.engine_id)).length;
    if (hosts > 0) {
      return { kind: 'push', version: target, hosts, installer: behind.length - hosts };
    }
  }
  return { kind: 'installer', hosts: behind.length };
}

/**
 * One sentence about a run, written to be read while it is happening.
 *
 * Says what is true rather than what is left, because "3 of 12" invites the
 * reading that the other nine are queued and safe — and a staged rollout that
 * fails stops, so they are neither queued nor going to happen.
 */
export function describeRollout(rollout: Rollout): string {
  const done = rollout.results.filter((result) => result.state === 'confirmed').length;
  const planned = rollout.planned.length;

  switch (rollout.state) {
    case 'running':
      return rollout.current
        ? `Upgrading ${short(rollout.current)} to ${rollout.version}. ${done} of ${planned} confirmed so far; each host is confirmed before the next one is touched.`
        : `Rolling out ${rollout.version} to ${planned} host${planned === 1 ? '' : 's'}, one at a time.`;
    case 'finished':
      return `${rollout.version} is on ${done} host${done === 1 ? '' : 's'}. Every one of them came back and said so.`;
    case 'stopped':
      return `Stopped after ${done} of ${planned}. The hosts not yet touched are still on the version they were.`;
    default:
      // The failure case, and the only one that names a host. `detail` is the
      // Controller's own sentence and carries the reason; repeating a summary
      // over it would bury the part that says what to do.
      return `The rollout of ${rollout.version} stopped. ${rollout.detail ?? ''}`.trim();
  }
}

/**
 * Whether a run ended in a way an operator has to look at.
 *
 * `stopped` is not a fault — somebody pressed the button — so it is reported
 * and not coloured. `failed` is a host refusing or not coming back, and it is
 * the one thing on this panel that should be hard to miss.
 */
export function rolloutFailed(rollout: Rollout): boolean {
  return rollout.state === 'failed';
}

/**
 * The per-host rows, in the order they happened, for a run worth expanding.
 *
 * Only ever the hosts a run actually reached. A list padded with the planned
 * hosts it never got to would say "pending" beside machines that a stopped run
 * is not going to touch.
 */
export function rolloutRows(
  rollout: Rollout,
): readonly { engineId: string; label: string; state: string; reason: string | null }[] {
  const rows = rollout.results.map((result) => ({
    engineId: result.engine_id,
    label: short(result.engine_id),
    state: result.state,
    reason: result.reason,
  }));
  if (rollout.state === 'running' && rollout.current) {
    rows.push({
      engineId: rollout.current,
      label: short(rollout.current),
      state: 'in progress',
      reason: null,
    });
  }
  return rows;
}

/** Engine ids are not names. Enough to tell two rows apart. */
function short(engineId: string): string {
  return engineId.length > 14 ? `${engineId.slice(0, 12)}…` : engineId;
}
