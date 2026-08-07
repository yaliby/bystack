/**
 * What the UI is allowed to claim about the system.
 *
 * Two independent facts decide this: whether the browser is attached to the
 * Controller (the WebSocket) and whether the Controller can still see the
 * infrastructure (`/healthz`). Reporting only the first is how a canvas full
 * of stale cards ends up labelled `live`.
 *
 * Pure, so the interesting cases — degraded provider, reachable Controller
 * with nothing to show, unreachable Controller — are unit tests rather than a
 * thing someone has to reproduce by unplugging a laptop.
 */

import type { ProviderHealth } from '../../../api/types';
import type { ConnectionState } from './useGraphStream';
import type { HealthState } from './useHealth';

export type Tone = 'good' | 'warn' | 'bad';

export interface SystemStatus {
  readonly tone: Tone;
  /** Short, for the status pill. */
  readonly label: string;
  /** One sentence naming what is wrong, or `null` when nothing is. */
  readonly detail: string | null;
  /** Whether this warrants stealing space for a banner. */
  readonly banner: boolean;
  /** Providers that are not currently serving, worst first. */
  readonly ailing: readonly ProviderHealth[];
}

const AILING_STATES = new Set(['degraded', 'failed', 'stopped']);

export function deriveStatus(connection: ConnectionState, health: HealthState): SystemStatus {
  // The browser's own link comes first: if it is down, whatever /healthz last
  // said is already history and the graph on screen is frozen.
  if (connection === 'offline' || connection === 'reconnecting') {
    return {
      tone: 'bad',
      label: connection === 'offline' ? 'offline' : 'reconnecting',
      detail: 'Disconnected from the Controller — the topology below is the last known state.',
      banner: true,
      ailing: [],
    };
  }

  if (health.kind === 'unreachable') {
    return {
      tone: 'bad',
      label: 'unreachable',
      detail: 'The Controller is not answering. It may be restarting.',
      banner: true,
      ailing: [],
    };
  }

  if (connection === 'connecting' || health.kind === 'pending') {
    return { tone: 'warn', label: 'connecting', detail: null, banner: false, ailing: [] };
  }

  const ailing = [...health.health.providers]
    .filter((provider) => AILING_STATES.has(provider.state))
    .sort((a, b) => severity(b.state) - severity(a.state));

  if (ailing.length > 0) {
    return {
      tone: 'warn',
      label: 'degraded',
      detail: describeAiling(ailing),
      banner: true,
      ailing,
    };
  }

  const syncing = health.health.providers.some((provider) => provider.state === 'syncing');
  if (syncing) {
    return { tone: 'warn', label: 'syncing', detail: null, banner: false, ailing: [] };
  }

  return { tone: 'good', label: 'live', detail: null, banner: false, ailing: [] };
}

/**
 * Why the canvas is empty, when it is.
 *
 * An empty canvas is ambiguous in exactly the way that matters: a host with no
 * containers and a host we cannot read look identical. Only the provider state
 * can tell them apart, so the overlay says which one this is.
 */
export interface EmptyExplanation {
  readonly title: string;
  readonly body: string;
  readonly tone: Tone;
}

export function explainEmpty(status: SystemStatus, health: HealthState): EmptyExplanation | null {
  if (status.ailing.length > 0) {
    const worst = status.ailing[0];
    return {
      tone: 'warn',
      title: `Cannot read ${worst.id}`,
      body:
        worst.detail ??
        `The ${worst.kind} provider is ${worst.state}. Nothing can be discovered until it recovers.`,
    };
  }

  if (status.tone === 'bad') {
    return {
      tone: 'bad',
      title: status.label === 'unreachable' ? 'Controller unreachable' : 'Disconnected',
      body: status.detail ?? '',
    };
  }

  // Still finding out. An overlay guessing here would flicker on every load.
  if (health.kind !== 'reached') return null;

  if (health.health.providers.length === 0) {
    return {
      tone: 'warn',
      title: 'No sources configured',
      body: 'The Controller started with nothing to discover. Check the `hosts:` block in your configuration.',
    };
  }

  return {
    tone: 'good',
    title: 'Nothing running',
    body: 'Every source is healthy and reports no containers. Start something and it will appear here.',
  };
}

function severity(state: ProviderHealth['state']): number {
  if (state === 'failed') return 3;
  if (state === 'degraded') return 2;
  if (state === 'stopped') return 1;
  return 0;
}

function describeAiling(ailing: readonly ProviderHealth[]): string {
  const [worst] = ailing;
  const rest = ailing.length - 1;
  const suffix = rest > 0 ? ` (and ${rest} more)` : '';
  const because = worst.detail ? `: ${worst.detail}` : '';
  return `Source \`${worst.id}\` is ${worst.state}${suffix}${because}`;
}
