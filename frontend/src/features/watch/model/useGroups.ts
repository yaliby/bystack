/**
 * The fleet's watch groups, and commands sent to them.
 *
 * Lifecycle and network only; what an answer *means* is in `groups.ts`.
 *
 * **Nothing here polls.** A watch list changes when this operator changes it,
 * and the one client that does is in the same tab — so a poll would be a
 * slower copy of something already known. `nonce` is how an edit says so:
 * `WatchPanel` bumps it, this refetches, and a fleet that nobody is editing
 * costs one request per page load.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import type {
  CommandKind,
  GroupCommandResult,
  Urn,
  WatchEntry,
} from '../../../api/types';
import { isMockMode } from '../../../mock/demoMode';
import { demoRunGroup, listWatchEntries } from '../../../mock/demoWatch';
import { indexGroups, type WatchGroup } from './groups';

export interface Groups {
  /** Every watched node in the fleet, pointed at the group it belongs to. */
  readonly byUrn: ReadonlyMap<Urn, WatchGroup>;
  readonly run: (
    kind: CommandKind,
    groupId: string,
    engineIds: readonly string[],
  ) => Promise<GroupCommandResult | null>;
  readonly busy: boolean;
  readonly error: string | null;
  readonly dismissError: () => void;
}

export function useGroups(
  baseUrl: string,
  nonce: number,
  resolveHostName: (engineId: string) => string | null,
): Groups {
  const [entries, setEntries] = useState<readonly WatchEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isMockMode()) {
      setEntries([...listWatchEntries()]);
      return;
    }
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch(new URL('/api/v1/agents/watch', baseUrl), {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`watch ${response.status}`);
        setEntries((await response.json()) as WatchEntry[]);
      } catch {
        // An empty index means no scope selector, which is the safe direction
        // to fail in: the per-host action bar still works, and nothing offers
        // to act on machines we could not confirm are in the group.
        if (!controller.signal.aborted) setEntries([]);
      }
    })();
    return () => controller.abort();
  }, [baseUrl, nonce]);

  // `resolveHostName` closes over the graph and changes on every delta, so the
  // index is memoised on the entries and rebuilt when names actually arrive
  // rather than on every frame of a busy canvas.
  const byUrn = useMemo(
    () => indexGroups(entries, resolveHostName),
    [entries, resolveHostName],
  );

  const run = useCallback(
    async (
      kind: CommandKind,
      groupId: string,
      engineIds: readonly string[],
    ): Promise<GroupCommandResult | null> => {
      setBusy(true);
      setError(null);
      try {
        if (isMockMode()) {
          return demoRunGroup(kind, groupId, engineIds);
        }
        const response = await fetch(new URL('/api/v1/commands/group', baseUrl), {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ kind, group_id: groupId, engine_ids: engineIds }),
        });
        if (!response.ok) {
          const body = (await response.json().catch(() => null)) as
            | { detail?: { message?: string } | string }
            | null;
          const detail = body?.detail;
          setError(
            typeof detail === 'string'
              ? detail
              : (detail?.message ?? `The Controller refused this (${response.status}).`),
          );
          return null;
        }
        return (await response.json()) as GroupCommandResult;
      } catch {
        setError('Could not reach the Controller.');
        return null;
      } finally {
        setBusy(false);
      }
    },
    [baseUrl],
  );

  return { byUrn, run, busy, error, dismissError: () => setError(null) };
}
