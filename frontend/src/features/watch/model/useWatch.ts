/**
 * One host's watch list, and the picker that fills it.
 *
 * Lifecycle and network only; every decision about what an answer *means* is
 * in `watch.ts`, which is pure.
 *
 * **Nothing here polls.** The list changes when this operator changes it, and
 * the *states* arrive on the delta stream like every other node — so a poll
 * would be a second, slower copy of a thing the canvas already has. The one
 * network call that is not an edit is the inventory, and it happens when a
 * person opens the picker or types in it: enumerating a machine is the single
 * most expensive thing this feature can ask a host to do, and `useLogs.ts` set
 * the precedent for what that means — opening the panel is the ask, and
 * clicking through a canvas must never be.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Inventory, MatchKind, WatchEntry, WatchKind, WatchList } from '../../../api/types';

/**
 * How long to wait after a keystroke before asking the host again.
 *
 * The filter runs on the managed machine (`agent/src/systemd.rs`), so every
 * keystroke that reaches it is a walk of somebody's unit table. A quarter of a
 * second is under the threshold where a filter feels laggy and above the rate
 * anybody types.
 */
const FILTER_DEBOUNCE_MS = 250;

export interface Watching {
  readonly entries: readonly WatchEntry[];
  readonly loaded: boolean;
  /** Whether the host currently holds this list. See `WatchList.delivered`. */
  readonly delivered: boolean;
  readonly detail: string | null;
  readonly error: string | null;
  readonly busy: boolean;
  readonly add: (draft: Draft) => Promise<boolean>;
  readonly remove: (entryId: string) => Promise<void>;
  readonly dismissError: () => void;
}

export interface Draft {
  readonly kind: WatchKind;
  readonly name: string;
  readonly match_kind: MatchKind | null;
  readonly pattern: string;
  readonly label: string;
}

export function useWatch(baseUrl: string, engineId: string | null): Watching {
  const [list, setList] = useState<WatchList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (engineId === null) return;
      try {
        const response = await fetch(
          new URL(`/api/v1/agents/${encodeURIComponent(engineId)}/watch`, baseUrl),
          { signal },
        );
        if (!response.ok) throw new Error(`watch ${response.status}`);
        setList((await response.json()) as WatchList);
      } catch (cause) {
        if (!signal?.aborted) setError(describe(cause));
      }
    },
    [baseUrl, engineId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setList(null);
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const add = useCallback(
    async (draft: Draft): Promise<boolean> => {
      if (engineId === null) return false;
      setBusy(true);
      setError(null);
      try {
        const response = await fetch(
          new URL(`/api/v1/agents/${encodeURIComponent(engineId)}/watch`, baseUrl),
          {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(draft),
          },
        );
        if (!response.ok) {
          // The Controller writes these to be shown to whoever typed the
          // thing (`core/ports/watch.py`), so they are surfaced verbatim
          // rather than replaced with a generic failure.
          const body = (await response.json().catch(() => null)) as { detail?: string } | null;
          throw new Error(body?.detail ?? `watch ${response.status}`);
        }
        setList((await response.json()) as WatchList);
        return true;
      } catch (cause) {
        setError(describe(cause));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [baseUrl, engineId],
  );

  const remove = useCallback(
    async (entryId: string) => {
      if (engineId === null) return;
      setBusy(true);
      try {
        const response = await fetch(
          new URL(
            `/api/v1/agents/${encodeURIComponent(engineId)}/watch/${encodeURIComponent(entryId)}`,
            baseUrl,
          ),
          { method: 'DELETE' },
        );
        if (!response.ok) throw new Error(`watch ${response.status}`);
        setList((await response.json()) as WatchList);
      } catch (cause) {
        setError(describe(cause));
      } finally {
        setBusy(false);
      }
    },
    [baseUrl, engineId],
  );

  return {
    entries: list?.entries ?? [],
    loaded: list !== null,
    delivered: list?.delivered ?? false,
    detail: list?.detail ?? null,
    error,
    busy,
    add,
    remove,
    dismissError: () => setError(null),
  };
}

/**
 * What could be watched here, asked of the host on demand.
 *
 * `enabled` is the whole contract: the request is made when the picker is
 * open and at no other time. A hook that fetched on mount would enumerate a
 * machine every time the panel rendered.
 */
export function useInventory(
  baseUrl: string,
  engineId: string | null,
  kind: WatchKind,
  filter: string,
  enabled: boolean,
): { readonly inventory: Inventory | null; readonly loading: boolean } {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [loading, setLoading] = useState(false);
  // The debounce timer, kept across renders so a burst of keystrokes cancels
  // its predecessor rather than queueing a request per character.
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!enabled || engineId === null) {
      setInventory(null);
      return;
    }
    const controller = new AbortController();
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      setLoading(true);
      const url = new URL(`/api/v1/agents/${encodeURIComponent(engineId)}/inventory`, baseUrl);
      url.searchParams.set('kind', kind);
      if (filter) url.searchParams.set('filter', filter);
      fetch(url, { signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error(`inventory ${response.status}`);
          setInventory((await response.json()) as Inventory);
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          // Shaped like a refusal from the agent rather than thrown, so the
          // panel has one place to read a reason from: an unreachable
          // Controller and an unreachable host are the same sentence to the
          // person looking at the dialog.
          setInventory({
            engine_id: engineId,
            kind,
            ok: false,
            reason: describe(cause),
            items: [],
            total: 0,
          });
        })
        .finally(() => setLoading(false));
    }, FILTER_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer.current);
      controller.abort();
    };
  }, [baseUrl, engineId, kind, filter, enabled]);

  return { inventory, loading };
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
