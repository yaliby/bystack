import { describe, expect, it } from 'vitest';
import type { Health, ProviderHealth } from '../../../api/types';
import { deriveStatus, explainEmpty } from './status';
import type { HealthState } from './useHealth';

function provider(partial: Partial<ProviderHealth> = {}): ProviderHealth {
  return {
    id: 'local',
    kind: 'docker',
    state: 'ready',
    detail: null,
    last_sync_at: 0,
    node_count: 0,
    metrics: {},
    ...partial,
  };
}

function reached(partial: Partial<Health> = {}): HealthState {
  return {
    kind: 'reached',
    health: {
      status: 'ok',
      seq: 1,
      node_count: 0,
      edge_count: 0,
      read_only: true,
      providers: [provider()],
      ...partial,
    },
  };
}

describe('deriveStatus', () => {
  it('reports live only when the socket and every provider are well', () => {
    const status = deriveStatus('live', reached());

    expect(status.label).toBe('live');
    expect(status.tone).toBe('good');
    expect(status.banner).toBe(false);
  });

  it('does not claim live while a provider is degraded', () => {
    // The bug this exists to prevent: an open WebSocket said `live` over a
    // canvas whose only source had stopped answering.
    const status = deriveStatus(
      'live',
      reached({
        status: 'degraded',
        providers: [provider({ state: 'degraded', detail: 'permission denied on docker.sock' })],
      }),
    );

    expect(status.label).toBe('degraded');
    expect(status.tone).toBe('warn');
    expect(status.banner).toBe(true);
    expect(status.detail).toContain('permission denied');
  });

  it('ranks the worst provider first', () => {
    const status = deriveStatus(
      'live',
      reached({
        providers: [
          provider({ id: 'a', state: 'stopped' }),
          provider({ id: 'b', state: 'failed' }),
          provider({ id: 'c', state: 'degraded' }),
        ],
      }),
    );

    expect(status.ailing.map((p) => p.id)).toEqual(['b', 'c', 'a']);
    expect(status.detail).toContain('and 2 more');
  });

  it('treats a lost socket as worse than a degraded provider', () => {
    // Health is a cached claim once the stream is gone; say so rather than
    // reporting whatever /healthz managed to answer last.
    const status = deriveStatus('reconnecting', reached());

    expect(status.tone).toBe('bad');
    expect(status.label).toBe('reconnecting');
    expect(status.banner).toBe(true);
  });

  it('separates an unreachable Controller from a degraded one', () => {
    const status = deriveStatus('live', { kind: 'unreachable' });

    expect(status.label).toBe('unreachable');
    expect(status.tone).toBe('bad');
  });

  it('stays quiet while the first answers are still in flight', () => {
    const status = deriveStatus('connecting', { kind: 'pending' });

    expect(status.banner).toBe(false);
    expect(status.tone).toBe('warn');
  });
});

describe('explainEmpty', () => {
  it('blames the provider when one cannot be read', () => {
    const health = reached({
      providers: [provider({ state: 'degraded', detail: 'permission denied on docker.sock' })],
    });
    const explanation = explainEmpty(deriveStatus('live', health), health)!;

    expect(explanation.title).toContain('local');
    expect(explanation.body).toContain('permission denied');
    expect(explanation.tone).toBe('warn');
  });

  it('says so plainly when everything is healthy and nothing runs', () => {
    const health = reached();
    const explanation = explainEmpty(deriveStatus('live', health), health)!;

    expect(explanation.tone).toBe('good');
    expect(explanation.title).toBe('Nothing running');
  });

  it('flags a Controller that was given nothing to discover', () => {
    const health = reached({ providers: [] });
    const explanation = explainEmpty(deriveStatus('live', health), health)!;

    expect(explanation.title).toBe('No sources configured');
  });

  it('offers no explanation before the first health answer', () => {
    const pending: HealthState = { kind: 'pending' };

    expect(explainEmpty(deriveStatus('connecting', pending), pending)).toBeNull();
  });
});
