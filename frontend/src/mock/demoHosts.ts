/**
 * Engine ids of the canned fleet.
 *
 * A leaf on purpose: `demoWatch.ts` (configuration), `demoSnapshot.ts` (graph)
 * and `demoGraph.ts` (discovery) all need these, and each imports one of the
 * others. Keeping the ids here is what stops that chain from closing into a
 * cycle, where whichever module loaded first would read a `const` from a
 * half-evaluated sibling and throw before React mounts.
 */

export const DEMO_E1 = 'e1';
export const DEMO_E2 = 'e2';
export const DEMO_E3 = 'e3';
