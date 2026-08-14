/**
 * Temporary canned topology for visual checks without a Controller.
 *
 * Activated via `?mock=1` on the Vite URL. Mirrors the layout in the
 * screenshot: MONITORING + BYSTACK stacks publishing onto one host, so the
 * exposed_on wires and port chips are on screen.
 */

import type { GraphEdge, GraphNode, SnapshotMessage } from '../api/types';

function node(
  partial: Partial<GraphNode> & Pick<GraphNode, 'urn' | 'kind' | 'name'>,
): GraphNode {
  return {
    source: 'mock',
    status: 'running',
    labels: {},
    attrs: {},
    observed_at: Date.now() / 1000,
    revision: 'mock',
    ...partial,
  };
}

function edge(
  kind: GraphEdge['kind'],
  src: string,
  dst: string,
  attrs: Record<string, unknown> = {},
): GraphEdge {
  return { key: `${kind}|${src}|${dst}`, kind, src, dst, source: 'mock', attrs };
}

const HOST = 'bystack:host:e1';
const MON = 'bystack:stack:e1/monitoring';
const BYS = 'bystack:stack:e1/bystack';

const GRAFANA = 'bystack:service:e1/monitoring/grafana';
const PROM = 'bystack:service:e1/monitoring/prometheus';
const CONTROLLER = 'bystack:service:e1/bystack/controller';

const CG = 'bystack:container:e1/c-grafana';
const CP = 'bystack:container:e1/c-prom';
const CC = 'bystack:container:e1/c-controller';

const VOL_PROM = 'bystack:volume:e1/9a9dd6aa';
const VOL_STATE = 'bystack:volume:e1/bystack-state';

export const DEMO_SNAPSHOT: SnapshotMessage = {
  type: 'snapshot',
  seq: 1,
  nodes: [
    node({ urn: HOST, kind: 'host', name: 'lowserv' }),

    node({ urn: MON, kind: 'stack', name: 'monitoring' }),
    node({
      urn: GRAFANA,
      kind: 'service',
      name: 'grafana',
      status: 'running',
    }),
    node({
      urn: PROM,
      kind: 'service',
      name: 'prometheus',
      status: 'running',
    }),
    node({
      urn: CG,
      kind: 'container',
      name: 'monitoring-grafana-1',
      attrs: {
        image: 'grafana/grafana:latest',
        ports: [
          { public: 3000, private: 3000 },
          { public: 3000, private: 3000 },
        ],
      },
    }),
    node({
      urn: CP,
      kind: 'container',
      name: 'monitoring-prometheus-1',
      attrs: {
        image: 'prom/prometheus:latest',
        ports: [
          { public: 9090, private: 9099 },
          { public: 9090, private: 9099 },
        ],
      },
    }),
    node({
      urn: VOL_PROM,
      kind: 'volume',
      name: '9a9dd6aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      labels: {},
    }),

    node({ urn: BYS, kind: 'stack', name: 'bystack' }),
    node({
      urn: CONTROLLER,
      kind: 'service',
      name: 'controller',
      status: 'running',
    }),
    node({
      urn: CC,
      kind: 'container',
      name: 'bystack-controller-1',
      attrs: {
        image: 'ghcr.io/yaliby/bystack:latest',
        ports: [
          { public: 8443, private: 8443 },
          { public: 8080, private: 8000 },
        ],
      },
    }),
    node({
      urn: VOL_STATE,
      kind: 'volume',
      name: 'bystack_bystack-state',
      labels: { 'com.docker.compose.volume': 'bystack-state' },
    }),
  ],
  edges: [
    edge('contains', MON, GRAFANA),
    edge('contains', MON, PROM),
    edge('realized_by', GRAFANA, CG),
    edge('realized_by', PROM, CP),
    edge('mounts', CP, VOL_PROM),
    edge('exposed_on', GRAFANA, HOST, {
      ports: [{ public: 3000, private: 3000 }],
      published: [3000],
    }),
    edge('exposed_on', PROM, HOST, {
      ports: [{ public: 9090, private: 9099 }],
      published: [9090],
    }),

    edge('contains', BYS, CONTROLLER),
    edge('realized_by', CONTROLLER, CC),
    edge('mounts', CC, VOL_STATE),
    edge('exposed_on', CONTROLLER, HOST, {
      ports: [
        { public: 8443, private: 8443 },
        { public: 8080, private: 8000 },
      ],
      published: [8443, 8080],
    }),
  ],
};
