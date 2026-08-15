import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode } from '../../../api/types';
import {
  KIND_LABEL,
  cardIdentityColor,
  cardPorts,
  cardSubtitle,
  edgePortLabel,
  edgeVerb,
  stackIdentityColor,
  statusOf,
  statusText,
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
  function node(
    status: string | null,
    attrs: Record<string, unknown> = {},
    kind: GraphNode['kind'] = 'container',
  ): GraphNode {
    return {
      urn: 'ctr:web',
      kind,
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

  it('does not treat a volume as running even when status says so', () => {
    expect(statusOf(node('running', {}, 'volume'))).toBe('neutral');
  });
});

describe('cardSubtitle', () => {
  function node(
    kind: GraphNode['kind'],
    name: string,
    attrs: Record<string, unknown> = {},
  ): GraphNode {
    return {
      urn: `bystack:${kind}:e1/${name}`,
      kind,
      name,
      source: 'local',
      status: null,
      labels: {},
      attrs,
      observed_at: 0,
      revision: 'r',
    };
  }

  it('separates the three workload kinds that share a card shape', () => {
    // `nginx` could be any of these three, and they are drawn identically.
    // The word on the second line is the only thing that tells them apart.
    expect(cardSubtitle(node('service', 'nginx', { image: 'nginx:1' }))).toBe(
      'compose service · nginx:1',
    );
    expect(
      cardSubtitle(node('unit', 'nginx.service', { sub_state: 'running', load_state: 'loaded' })),
    ).toBe('systemd unit · running');
    expect(
      cardSubtitle(
        node('process', 'worker', { pattern: '/usr/local/bin/order-worker', match: 'exec' }),
      ),
    ).toBe('process · exec order-worker');
  });

  it('keeps a process pattern readable instead of ellipsizing its useful end', () => {
    const long = node('process', 'worker', {
      pattern: '/opt/vendor/very/long/path/to/order-worker',
      match: 'exec',
    });
    expect(cardSubtitle(long)).toBe('process · exec order-worker');
  });

  it('takes the kind from the service and the image from its container', () => {
    // The folded card is a compose service; what it runs is the container's.
    const service = node('service', 'caddy');
    const container = node('container', 'edge-caddy-1', { image: 'caddy:2' });
    expect(cardSubtitle(service, container)).toBe('compose service · caddy:2');
  });

  it('names a bare container as a container', () => {
    expect(cardSubtitle(node('container', 'c', { image: 'caddy:2' }))).toBe('container · caddy:2');
  });
});

describe('edgeVerb', () => {
  it('reads contains from the child as membership, not ownership', () => {
    expect(edgeVerb('contains', 'out')).toBe('contains');
    expect(edgeVerb('contains', 'in')).toBe('in');
  });

  it('reads mounts from the volume as mounted by', () => {
    expect(edgeVerb('mounts', 'out')).toBe('mounts');
    expect(edgeVerb('mounts', 'in')).toBe('mounted by');
  });
});

describe('statusText', () => {
  function node(kind: GraphNode['kind'], status: string | null): GraphNode {
    return {
      urn: `bystack:${kind}:e1/x`,
      kind,
      name: 'x',
      source: 'local',
      status,
      labels: {},
      attrs: {},
      observed_at: 0,
      revision: 'r',
    };
  }

  it('uses systemd vocabulary for units', () => {
    expect(statusText(node('unit', 'active'))).toBe('Active');
    expect(statusText(node('unit', 'not-found'))).toBe('Not-found');
  });

  it('does not call a volume Running', () => {
    expect(statusText(node('volume', 'running'))).toBe('No state');
  });
});

describe('KIND_LABEL', () => {
  it('qualifies every name that would otherwise collide on "service"', () => {
    expect(KIND_LABEL.unit).toBe('systemd unit');
    expect(KIND_LABEL.service).toBe('Compose service');
    expect(KIND_LABEL.container).toBe('Container');
  });

  it('never labels two kinds with the same word', () => {
    const labels = Object.values(KIND_LABEL).map((label) => label.toLowerCase());
    expect(new Set(labels).size).toBe(labels.length);
  });
});
