/**
 * Temporary canned topology for visual checks without a Controller.
 *
 * Activated via `?mock=1` on the Vite URL (default on in DEV). Three hosts,
 * the original monitoring + bystack stacks on the first, and watched units /
 * processes that mirror `demoWatch.ts` — including an nginx group across the
 * fleet so the ActionBar scope picker has something real to offer.
 */

import type { GraphEdge, GraphNode, SnapshotMessage } from '../api/types';
import { DEMO_E1, DEMO_E2, DEMO_E3 } from './demoHosts';

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

const HOST1 = `bystack:host:${DEMO_E1}`;
const HOST2 = `bystack:host:${DEMO_E2}`;
const HOST3 = `bystack:host:${DEMO_E3}`;

const MON = `bystack:stack:${DEMO_E1}/monitoring`;
const BYS = `bystack:stack:${DEMO_E1}/bystack`;
const EDGE_STACK = `bystack:stack:${DEMO_E2}/edge`;

const GRAFANA = `bystack:service:${DEMO_E1}/monitoring/grafana`;
const PROM = `bystack:service:${DEMO_E1}/monitoring/prometheus`;
const CONTROLLER = `bystack:service:${DEMO_E1}/bystack/controller`;
const CADDY = `bystack:service:${DEMO_E2}/edge/caddy`;

const CG = `bystack:container:${DEMO_E1}/c-grafana`;
const CP = `bystack:container:${DEMO_E1}/c-prom`;
const CC = `bystack:container:${DEMO_E1}/c-controller`;
const CAD = `bystack:container:${DEMO_E2}/c-caddy`;

const VOL_PROM = `bystack:volume:${DEMO_E1}/9a9dd6aa`;
const VOL_STATE = `bystack:volume:${DEMO_E1}/bystack-state`;

const NGINX1 = `bystack:unit:${DEMO_E1}/nginx.service`;
const NGINX2 = `bystack:unit:${DEMO_E2}/nginx.service`;
const NGINX3 = `bystack:unit:${DEMO_E3}/nginx.service`;
const SSHD = `bystack:unit:${DEMO_E1}/sshd.service`;
const POSTGRES = `bystack:unit:${DEMO_E3}/postgresql.service`;
const WORKER1 = `bystack:process:${DEMO_E1}/w-worker-e1`;
const WORKER2 = `bystack:process:${DEMO_E2}/w-worker-e2`;

export const DEMO_SNAPSHOT: SnapshotMessage = {
  type: 'snapshot',
  seq: 1,
  nodes: [
    node({ urn: HOST1, kind: 'host', name: 'lowserv' }),
    node({ urn: HOST2, kind: 'host', name: 'edge-01' }),
    node({ urn: HOST3, kind: 'host', name: 'shop-db' }),

    node({ urn: MON, kind: 'stack', name: 'monitoring' }),
    node({ urn: GRAFANA, kind: 'service', name: 'grafana', status: 'running' }),
    node({ urn: PROM, kind: 'service', name: 'prometheus', status: 'running' }),
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
      status: null,
      labels: {},
    }),

    node({ urn: BYS, kind: 'stack', name: 'bystack' }),
    node({ urn: CONTROLLER, kind: 'service', name: 'controller', status: 'running' }),
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
      status: null,
      labels: { 'com.docker.compose.volume': 'bystack-state' },
    }),

    node({ urn: EDGE_STACK, kind: 'stack', name: 'edge' }),
    node({ urn: CADDY, kind: 'service', name: 'caddy', status: 'running' }),
    node({
      urn: CAD,
      kind: 'container',
      name: 'edge-caddy-1',
      attrs: {
        image: 'caddy:2',
        ports: [{ public: 443, private: 443 }],
      },
    }),

    // Watched units — nginx is the fan-out group across three hosts.
    node({
      urn: NGINX1,
      kind: 'unit',
      name: 'nginx.service',
      status: 'active',
      attrs: {
        description: 'A high performance web server and a reverse proxy server',
        load_state: 'loaded',
        sub_state: 'running',
        unit_file_state: 'enabled',
        main_pid: 1204,
        active_since: Date.now() / 1000 - 86_400,
      },
    }),
    node({
      urn: NGINX2,
      kind: 'unit',
      name: 'nginx.service',
      status: 'active',
      attrs: {
        description: 'A high performance web server and a reverse proxy server',
        load_state: 'loaded',
        sub_state: 'running',
        unit_file_state: 'enabled',
        main_pid: 988,
        active_since: Date.now() / 1000 - 40_000,
      },
    }),
    node({
      urn: NGINX3,
      kind: 'unit',
      name: 'nginx.service',
      status: 'not-found',
      attrs: {
        description: null,
        load_state: 'not-found',
        sub_state: 'dead',
        unit_file_state: null,
        main_pid: null,
      },
    }),
    node({
      urn: SSHD,
      kind: 'unit',
      name: 'sshd.service',
      status: 'active',
      attrs: {
        description: 'OpenSSH server daemon',
        load_state: 'loaded',
        sub_state: 'running',
        unit_file_state: 'enabled',
        main_pid: 880,
        active_since: Date.now() / 1000 - 200_000,
      },
    }),
    node({
      urn: POSTGRES,
      kind: 'unit',
      name: 'postgresql.service',
      status: 'active',
      attrs: {
        description: 'PostgreSQL RDBMS',
        load_state: 'loaded',
        sub_state: 'running',
        unit_file_state: 'enabled',
        main_pid: 2102,
        active_since: Date.now() / 1000 - 500_000,
      },
    }),

    // Watched processes — order-worker is a two-host group.
    node({
      urn: WORKER1,
      kind: 'process',
      name: 'order-worker',
      status: 'running',
      attrs: {
        match: 'exec',
        pattern: '/usr/local/bin/order-worker',
        pids: [4412],
        instances: 1,
        matched: 1,
        cmdline: '/usr/local/bin/order-worker --queue=orders',
        uid: 1000,
      },
    }),
    node({
      urn: WORKER2,
      kind: 'process',
      name: 'order-worker',
      status: 'running',
      attrs: {
        match: 'exec',
        pattern: '/usr/local/bin/order-worker',
        pids: [3310, 3311],
        instances: 2,
        matched: 2,
        cmdline: '/usr/local/bin/order-worker --queue=orders',
        uid: 1000,
      },
    }),
  ],
  edges: [
    edge('contains', MON, GRAFANA),
    edge('contains', MON, PROM),
    edge('realized_by', GRAFANA, CG),
    edge('realized_by', PROM, CP),
    edge('mounts', CP, VOL_PROM),
    edge('exposed_on', GRAFANA, HOST1, {
      ports: [{ public: 3000, private: 3000 }],
      published: [3000],
    }),
    edge('exposed_on', PROM, HOST1, {
      ports: [{ public: 9090, private: 9099 }],
      published: [9090],
    }),

    edge('contains', BYS, CONTROLLER),
    edge('realized_by', CONTROLLER, CC),
    edge('mounts', CC, VOL_STATE),
    edge('exposed_on', CONTROLLER, HOST1, {
      ports: [
        { public: 8443, private: 8443 },
        { public: 8080, private: 8000 },
      ],
      published: [8443, 8080],
    }),

    edge('contains', EDGE_STACK, CADDY),
    edge('realized_by', CADDY, CAD),
    edge('exposed_on', CADDY, HOST2, {
      ports: [{ public: 443, private: 443 }],
      published: [443],
    }),

    edge('hosts', HOST1, NGINX1),
    edge('hosts', HOST2, NGINX2),
    edge('hosts', HOST3, NGINX3),
    edge('hosts', HOST1, SSHD),
    edge('hosts', HOST3, POSTGRES),
    edge('hosts', HOST1, WORKER1),
    edge('hosts', HOST2, WORKER2),
  ],
};
