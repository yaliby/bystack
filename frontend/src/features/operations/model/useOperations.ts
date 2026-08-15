/**
 * Operations for the selected node.
 *
 * Two jobs: keep the available-action list current for whatever is selected,
 * and run a command when asked. Everything about *what the result means* is
 * in `operations.ts`, which is pure; this file is lifecycle and network only.
 *
 * The action list is fetched, not computed. The Controller owns the policy —
 * which commands a container's state permits, whether we are read-only,
 * whether the host is even connected — and a second copy of that policy here
 * would drift from it within a release, in the direction of offering buttons
 * that cannot work.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Actions, CommandKind, CommandResult, GraphNode, Urn } from '../../../api/types';
import { isMockMode } from '../../../mock/demoMode';
import { demoActions, demoRunCommand } from '../../../mock/demoWatch';
import {
  CONFIRM_TIMEOUT_MS,
  OPERABLE_KINDS,
  phaseOf,
  witness,
  type Confirmation,
  type Phase,
} from './operations';

export interface Operations {
  readonly actions: Actions | null;
  readonly phase: Phase | null;
  readonly busy: boolean;
  readonly run: (kind: CommandKind) => Promise<void>;
  readonly dismiss: () => void;
}

const IDLE: Actions = { target: '', kind: '', targets: [], commands: [], reason: null, detail: null };

export function useOperations(
  baseUrl: string,
  target: Urn | null,
  nodes: ReadonlyMap<Urn, GraphNode>,
): Operations {
  const [actions, setActions] = useState<Actions | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [running, setRunning] = useState<CommandKind | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [, forceTick] = useState(0);

  // The graph changes on every delta. Reading it through a ref keeps it out
  // of the fetch effect's dependencies — otherwise selecting a node on a busy
  // cluster would re-request its actions several times a second.
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;

  // -- available actions -------------------------------------------------

  // `revision` and not the node itself: the object identity changes on every
  // snapshot, the revision only when the content did. Refetching actions is
  // exactly right when a container transitions (stop must become start) and
  // pure waste otherwise.
  const revision = target ? (nodes.get(target)?.revision ?? '') : '';

  // A coarse structural filter, so clicking through hosts, networks and
  // images on a big canvas does not issue a request per selection to be told
  // each time that they have no lifecycle.
  //
  // This is not the state policy duplicated — that stays on the Controller.
  // "A network is not a workload" is a fact about the model that cannot drift;
  // "a paused container can be resumed" is a policy that can, and is not here.
  const kind = target ? (nodes.get(target)?.kind ?? '') : '';
  const operable = OPERABLE_KINDS.has(kind);

  useEffect(() => {
    if (!target || !operable) {
      setActions(null);
      return;
    }

    if (isMockMode()) {
      const node = nodesRef.current.get(target);
      setActions(demoActions(target, node?.kind ?? kind, node?.status ?? null));
      return;
    }

    const controller = new AbortController();
    void (async () => {
      try {
        const url = new URL('/api/v1/commands/actions', baseUrl);
        url.searchParams.set('urn', target);
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`actions ${response.status}`);
        setActions((await response.json()) as Actions);
      } catch {
        // A failed lookup means no buttons, which is the safe direction to
        // fail in: we would rather offer nothing than offer an action the
        // Controller has not agreed to.
        if (!controller.signal.aborted) setActions(IDLE);
      }
    })();

    return () => controller.abort();
  }, [baseUrl, target, revision, operable, kind]);

  // Selecting something else abandons the previous command's result. It
  // belongs to that node, and carrying it across would attribute one node's
  // failure to another.
  useEffect(() => {
    setConfirmation(null);
    setError(null);
  }, [target]);

  // -- confirmation window ------------------------------------------------

  // The `confirming` phase resolves either because a delta arrived (a render
  // we get for free) or because it never did. Only the second needs a timer,
  // and it fires once rather than polling.
  useEffect(() => {
    if (!confirmation) return;
    const elapsed = Date.now() - confirmation.since;
    if (elapsed >= CONFIRM_TIMEOUT_MS) return;
    const timer = window.setTimeout(() => forceTick((n) => n + 1), CONFIRM_TIMEOUT_MS - elapsed);
    return () => window.clearTimeout(timer);
  }, [confirmation]);

  // -- execution ----------------------------------------------------------

  const run = useCallback(
    async (kind: CommandKind) => {
      if (!target) return;
      setRunning(kind);
      setError(null);
      setConfirmation(null);
      try {
        if (isMockMode()) {
          const result = demoRunCommand(kind, target);
          setConfirmation(witness(result, nodesRef.current, Date.now()));
          return;
        }
        const response = await fetch(new URL('/api/v1/commands', baseUrl), {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ kind, target }),
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
          return;
        }

        const result = (await response.json()) as CommandResult;
        // Witness the graph *now*, before any delta caused by this command
        // can land. A revision captured a moment later might already be the
        // post-change one, and the UI would report confirmation it never saw.
        setConfirmation(witness(result, nodesRef.current, Date.now()));
      } catch {
        setError('Could not reach the Controller.');
      } finally {
        setRunning(null);
      }
    },
    [baseUrl, target],
  );

  const dismiss = useCallback(() => {
    setConfirmation(null);
    setError(null);
  }, []);

  const phase: Phase | null = running
    ? { kind: 'running', command: running }
    : error
      ? { kind: 'error', message: error }
      : confirmation
        ? phaseOf(confirmation, nodes, Date.now())
        : null;

  return { actions, phase, busy: running !== null, run, dismiss };
}
