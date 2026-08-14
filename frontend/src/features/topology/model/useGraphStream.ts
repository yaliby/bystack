/**
 * Live graph subscription.
 *
 * Owns exactly one concern: keeping a `GraphState` current from the server's
 * snapshot + delta stream, and reconnecting when that breaks. All the state
 * transition rules live in `graphStore.ts`; this hook is lifecycle only.
 */

import { useEffect, useRef, useState } from 'react';
import type { StreamMessage } from '../../../api/types';
import { DEMO_SNAPSHOT } from '../../../mock/demoSnapshot';
import { clearStickyRoutes } from '../layout/liveEdges';
import { EMPTY_GRAPH, applyDelta, applySnapshot, type GraphState } from './graphStore';

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

export interface GraphStream {
  readonly graph: GraphState;
  readonly connection: ConnectionState;
  /** Incremented whenever a gap forced a resnapshot. Surfaced for diagnostics. */
  readonly resyncs: number;
}

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 15_000;

/** Dev mock: `?mock=1`, or `?mock=0` to force the real stream. */
function useMockGraph(): boolean {
  if (!import.meta.env.DEV) return false;
  const flag = new URLSearchParams(window.location.search).get('mock');
  if (flag === '0') return false;
  // Default on in dev — there is often no Controller on the side.
  return flag === null || flag === '1' || flag === '';
}

export function useGraphStream(baseUrl: string, sources?: readonly string[]): GraphStream {
  const [graph, setGraph] = useState<GraphState>(EMPTY_GRAPH);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [resyncs, setResyncs] = useState(0);

  // Read inside the effect but deliberately not a dependency: reconnect
  // backoff must not reset the socket every time a delta lands.
  const graphRef = useRef(graph);
  graphRef.current = graph;

  const scope = sources?.join(',') ?? '';
  const mock = useMockGraph();

  useEffect(() => {
    if (mock) {
      clearStickyRoutes();
      setGraph(applySnapshot(DEMO_SNAPSHOT));
      setConnection('live');
      return () => setConnection('offline');
    }

    let socket: WebSocket | null = null;
    let retryTimer: number | undefined;
    let backoff = RECONNECT_MIN_MS;
    let disposed = false;

    const url = new URL('/api/v1/stream', baseUrl);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    if (scope) {
      for (const source of scope.split(',')) url.searchParams.append('sources', source);
    }

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(url);

      socket.onopen = () => {
        backoff = RECONNECT_MIN_MS;
        setConnection('live');
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        const message = JSON.parse(event.data) as StreamMessage;

        if (message.type === 'snapshot') {
          setGraph(applySnapshot(message));
          return;
        }

        const result = applyDelta(graphRef.current, message);
        if (result.ok) {
          if (result.changed) setGraph(result.state);
          return;
        }
        if (result.reason === 'gap') {
          // We cannot reconstruct what we missed. Dropping the socket makes
          // the server send a fresh snapshot on reconnect, which is the only
          // correct recovery and is cheap by design.
          setResyncs((count) => count + 1);
          socket?.close();
        }
      };

      socket.onclose = () => {
        if (disposed) return;
        setConnection('reconnecting');
        retryTimer = window.setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
      };

      socket.onerror = () => socket?.close();
    };

    connect();

    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      // Detach handlers before closing: onclose would otherwise schedule a
      // reconnect for a component that is already gone.
      if (socket) {
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
      setConnection('offline');
    };
  }, [baseUrl, scope, mock]);

  return { graph, connection, resyncs };
}
