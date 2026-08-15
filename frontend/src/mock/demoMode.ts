/**
 * Whether the Vite app is painting the canned demo instead of a Controller.
 *
 * One predicate, used by every hook that would otherwise fetch — so the demo
 * cannot half-live (graph from the can, fleet from a dead port).
 */

export function isMockMode(): boolean {
  if (!import.meta.env.DEV) return false;
  return new URLSearchParams(window.location.search).get('mock') !== '0';
}
