import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode } from '../../../api/types';
import {
  cardIdentityColor,
  cardPorts,
  edgePortLabel,
  stackIdentityColor,
  statusOf,
} from './theme';

function container(ports: unknown): GraphNode {
  return {
    urn: 'ctr:web',
    kind: 'container',
    name: 'web',
    source: 'local',
    status: 'running',
    labels: {},
    attrs: { ports },
    observed_at: 0,
    revision: 'r',
  };
}

describe('cardPorts', () => {
  it('collapses the IPv4 and IPv6 binding of one published port', () => {
    const node = container([
      { private: 5432, public: 15432, protocol: 'tcp', host_ip: '0.0.0.0' },
      { private: 5432, public: 15432, protocol: 'tcp', host_ip: '::' },
    ]);
    expect(cardPorts(node)).toBe(':15432 → 5432');
  });

  it('keeps distinct mappings', () => {
    const node = container([
      { private: 80, public: 18080, host_ip: '0.0.0.0' },
      { private: 443, public: 18443, host_ip: '0.0.0.0' },
    ]);
    expect(cardPorts(node)).toBe(':18080 → 80  :18443 → 443');
  });

  it('ignores unpublished ports', () => {
    expect(cardPorts(container([{ private: 6379 }]))).toBeNull();
    expect(cardPorts(container([]))).toBeNull();
  });
});

describe('edgePortLabel', () => {
  function exposedOn(attrs: Record<string, unknown>): GraphEdge {
    return {
      key: 'exposed_on|ctr:web|host:h',
      kind: 'exposed_on',
      src: 'ctr:web',
      dst: 'host:h',
      source: 'local',
      attrs,
    };
  }

  it('labels a single published port', () => {
    expect(edgePortLabel(exposedOn({ published: [18080] }))).toBe(':18080');
  });

  it('prefers public → private mapping when ports are present', () => {
    expect(
      edgePortLabel(
        exposedOn({
          published: [15432],
          ports: [
            { private: 5432, public: 15432, host_ip: '0.0.0.0' },
            { private: 5432, public: 15432, host_ip: '::' },
          ],
        }),
      ),
    ).toBe(':15432 → 5432');
  });

  it('counts the rest rather than growing the chip', () => {
    expect(edgePortLabel(exposedOn({ published: [8443, 8080, 9000] }))).toBe(':8443 +2');
  });

  it('has nothing to say about a link with no ports', () => {
    expect(edgePortLabel(exposedOn({ published: [] }))).toBeNull();
    expect(edgePortLabel(exposedOn({}))).toBeNull();
  });
});

describe('cardIdentityColor', () => {
  it('gives services stable distinct accents from their name', () => {
    const web = cardIdentityColor({ urn: 'svc:web', name: 'web', kind: 'service' });
    const api = cardIdentityColor({ urn: 'svc:api', name: 'api', kind: 'service' });
    expect(web).toBeTruthy();
    expect(api).toBeTruthy();
    expect(web).not.toBe(api);
    expect(cardIdentityColor({ urn: 'svc:web-other', name: 'web', kind: 'service' })).toBe(web);
  });

  it('skips non-container kinds', () => {
    expect(cardIdentityColor({ urn: 'vol:x', name: 'data', kind: 'volume' })).toBeNull();
  });
});

describe('stackIdentityColor', () => {
  it('maps common stack roles to fixed hues', () => {
    expect(stackIdentityColor('backend')).toBe('#2ec8ff');
    expect(stackIdentityColor('FRONTEND')).toBe('#d070ff');
    expect(stackIdentityColor('data')).toBe('#ffb400');
  });

  it('keeps unknown names stable', () => {
    expect(stackIdentityColor('payments')).toBe(stackIdentityColor('payments'));
    expect(stackIdentityColor('payments')).not.toBe(stackIdentityColor('billing'));
  });
});

describe('statusOf', () => {
  function node(status: string, attrs: Record<string, unknown> = {}): GraphNode {
    return {
      urn: 'ctr:web',
      kind: 'container',
      name: 'web',
      source: 'local',
      status,
      labels: {},
      attrs,
      observed_at: 0,
      revision: 'r',
    };
  }

  it('draws a running container as good', () => {
    expect(statusOf(node('running'))).toBe('good');
  });

  it('refuses to draw a container failing its healthcheck as good', () => {
    // The state is still `running` and that is not wrong -- the engine has
    // not stopped it. Green is what would be wrong, because an operator
    // reads the absence of a warning as the absence of a problem.
    expect(statusOf(node('running', { health: 'unhealthy' }))).toBe('warning');
  });

  it('leaves a starting healthcheck alone', () => {
    // Every healthchecked container passes through this on the way up, so
    // warning on it would make the first seconds of every deploy look broken.
    expect(statusOf(node('running', { health: 'starting' }))).toBe('good');
    expect(statusOf(node('running', { health: 'healthy' }))).toBe('good');
  });

  it('still reports a stopped container as critical, healthy or not', () => {
    expect(statusOf(node('exited', { health: 'healthy' }))).toBe('critical');
  });
});
