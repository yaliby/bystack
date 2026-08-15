/**
 * The mock layer's load order, which once blanked the canvas.
 *
 * `demoWatch` needs discovery from `demoGraph`, `demoGraph` seeds itself from
 * `demoSnapshot`, and `demoSnapshot` names its hosts. When that last need was
 * met by importing from `demoWatch`, the chain closed: a hook reaching for the
 * watch panel first left `demoSnapshot` reading an engine id out of a module
 * still half-evaluated, which throws before React mounts. The host ids live in
 * `demoHosts` for that reason, and this file fails if they move back.
 *
 * The import below must stay first — that is the whole test.
 */

import { describe, expect, it } from 'vitest';
import { listWatchEntries } from './demoWatch';
import { demoGraphSnapshot } from './demoGraph';
import { DEMO_SNAPSHOT } from './demoSnapshot';

describe('the canned mock layer', () => {
  it('initialises when the watch panel is imported before the canvas', () => {
    expect(listWatchEntries().length).toBeGreaterThan(0);
    expect(DEMO_SNAPSHOT.nodes.length).toBeGreaterThan(0);
    expect(demoGraphSnapshot().nodes).toHaveLength(DEMO_SNAPSHOT.nodes.length);

    // A cycle throws in the browser but merely yields `undefined` under the
    // test transform, so name the symptom rather than trusting the throw.
    const unnamed = DEMO_SNAPSHOT.nodes.filter((node) => node.urn.includes('undefined'));
    expect(unnamed.map((node) => node.urn)).toEqual([]);
  });

  it('names every watched unit and process on a host that exists', () => {
    const hosts = new Set(
      DEMO_SNAPSHOT.nodes.filter((node) => node.kind === 'host').map((node) => node.urn),
    );
    const watched = DEMO_SNAPSHOT.nodes.filter(
      (node) => node.kind === 'unit' || node.kind === 'process',
    );

    expect(watched.length).toBeGreaterThan(0);
    for (const node of watched) {
      const anchor = DEMO_SNAPSHOT.edges.find(
        (edge) => edge.kind === 'hosts' && edge.dst === node.urn,
      );
      expect(anchor, `${node.urn} has no host`).toBeDefined();
      expect(hosts.has(anchor!.src)).toBe(true);
    }
  });
});
