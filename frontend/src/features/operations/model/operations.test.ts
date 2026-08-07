import { describe, expect, it } from 'vitest';
import type { CommandResult, CommandStatus, GraphNode, Urn } from '../../../api/types';
import {
  CONFIRM_TIMEOUT_MS,
  confirmed,
  confirmationText,
  needsConfirmation,
  phaseOf,
  summarize,
  witness,
} from './operations';

const WEB_1 = 'bystack:container:e1/aaa' as Urn;
const WEB_2 = 'bystack:container:e1/bbb' as Urn;

function node(urn: Urn, revision: string, name = 'web'): GraphNode {
  return {
    urn,
    kind: 'container',
    name,
    source: 'docker-a',
    status: 'running',
    labels: {},
    attrs: {},
    observed_at: 0,
    revision,
  };
}

function graph(...nodes: GraphNode[]): ReadonlyMap<Urn, GraphNode> {
  return new Map(nodes.map((n) => [n.urn, n]));
}

function result(
  outcomes: ReadonlyArray<[Urn, CommandStatus, string?]>,
  status: CommandStatus = 'succeeded',
): CommandResult {
  return {
    id: 'cmd1',
    kind: 'restart',
    target: WEB_1,
    status,
    actor: 'anonymous',
    requested_at: 0,
    duration_ms: 12,
    outcomes: outcomes.map(([target, outcomeStatus, detail]) => ({
      target,
      status: outcomeStatus,
      detail: detail ?? null,
      duration_ms: 5,
    })),
  };
}

describe('waiting for discovery to confirm', () => {
  it('is not confirmed while the target still has the revision we saw', () => {
    const before = graph(node(WEB_1, 'rev-1'));
    const pending = witness(result([[WEB_1, 'succeeded']]), before, 0);

    expect(confirmed(pending, before)).toBe(false);
  });

  it('is confirmed once the content revision changes', () => {
    const before = graph(node(WEB_1, 'rev-1'));
    const pending = witness(result([[WEB_1, 'succeeded']]), before, 0);

    expect(confirmed(pending, graph(node(WEB_1, 'rev-2')))).toBe(true);
  });

  it('is confirmed when the container was recreated rather than restarted', () => {
    // A `compose up` replaces the container under a new id. It leaving the
    // graph is confirmation that something happened, not a missing answer.
    const before = graph(node(WEB_1, 'rev-1'));
    const pending = witness(result([[WEB_1, 'succeeded']]), before, 0);

    expect(confirmed(pending, graph())).toBe(true);
  });

  it('does not wait on targets that were already in the requested state', () => {
    // A `noop` target's revision will never change, so witnessing it would
    // leave the UI pending forever on a command correctly reported as
    // having done nothing.
    const before = graph(node(WEB_1, 'rev-1'), node(WEB_2, 'rev-1'));
    const pending = witness(
      result([
        [WEB_1, 'noop'],
        [WEB_2, 'noop'],
      ]),
      before,
      0,
    );

    expect(pending.witnessed.size).toBe(0);
    expect(confirmed(pending, before)).toBe(true);
  });

  it('waits for every changed target, not just the first', () => {
    const before = graph(node(WEB_1, 'rev-1'), node(WEB_2, 'rev-1'));
    const pending = witness(
      result([
        [WEB_1, 'succeeded'],
        [WEB_2, 'succeeded'],
      ]),
      before,
      0,
    );

    const halfway = graph(node(WEB_1, 'rev-2'), node(WEB_2, 'rev-1'));
    expect(confirmed(pending, halfway)).toBe(false);
    expect(confirmed(pending, graph(node(WEB_1, 'rev-2'), node(WEB_2, 'rev-2')))).toBe(true);
  });
});

describe('phases', () => {
  it('reports confirming between the engine answering and the graph moving', () => {
    const before = graph(node(WEB_1, 'rev-1'));
    const pending = witness(result([[WEB_1, 'succeeded']]), before, 0);

    expect(phaseOf(pending, before, 500).kind).toBe('confirming');
  });

  it('gives up waiting rather than spinning forever', () => {
    // Past this point something is wrong — the host dropped its event stream,
    // or the change was reverted — and a spinner would imply we still expect
    // an answer we no longer expect.
    const before = graph(node(WEB_1, 'rev-1'));
    const pending = witness(result([[WEB_1, 'succeeded']]), before, 0);

    expect(phaseOf(pending, before, CONFIRM_TIMEOUT_MS + 1).kind).toBe('unconfirmed');
  });

  it('resolves immediately when the command failed', () => {
    // The engine already told us nothing changed. Waiting for a delta that
    // will never arrive would hide the error behind a spinner.
    const before = graph(node(WEB_1, 'rev-1'));
    const pending = witness(result([[WEB_1, 'failed', 'no such container']], 'failed'), before, 0);

    expect(phaseOf(pending, before, 500).kind).toBe('done');
  });
});

describe('summaries', () => {
  it('names the failing target rather than counting failures', () => {
    const summary = summarize(
      result(
        [
          [WEB_1, 'succeeded'],
          [WEB_2, 'failed', 'container is not running'],
        ],
        'failed',
      ),
      (urn) => (urn === WEB_2 ? 'shop-db-1' : 'shop-web-1'),
    );

    expect(summary).toBe('shop-db-1: container is not running');
  });

  it('counts containers on success', () => {
    const summary = summarize(
      result([
        [WEB_1, 'succeeded'],
        [WEB_2, 'succeeded'],
      ]),
      (urn) => urn,
    );

    expect(summary).toBe('Applied · 2 containers');
  });
});

describe('confirmation prompts', () => {
  const actions = (count: number) => ({
    target: WEB_1,
    kind: 'service',
    targets: Array.from({ length: count }, (_, index) => `${WEB_1}${index}` as Urn),
    commands: [] as const,
    reason: null,
    detail: null,
  });

  it('always confirms kill', () => {
    expect(needsConfirmation('kill', actions(1))).toBe(true);
  });

  it('confirms a stop that takes down more than one container', () => {
    expect(needsConfirmation('stop', actions(1))).toBe(false);
    expect(needsConfirmation('stop', actions(4))).toBe(true);
  });

  it('never blocks restart, the incident action', () => {
    // A confirmation dialog in the middle of an outage is an obstacle, not a
    // safeguard.
    expect(needsConfirmation('restart', actions(9))).toBe(false);
  });

  it('states the blast radius in the prompt', () => {
    expect(confirmationText('kill', actions(4), 'shop')).toBe('Kill shop (4 containers)?');
    expect(confirmationText('kill', actions(1), 'web')).toBe('Kill web?');
  });
});
