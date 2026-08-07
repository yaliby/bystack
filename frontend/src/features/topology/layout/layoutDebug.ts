/**
 * Causal debug trail for interactive layout.
 *
 * Enable:  localStorage.setItem('bystack:debug','1')  or  ?debug=1
 * Disable: localStorage.setItem('bystack:debug','0')
 * Dump:    window.__bystackDebug.dump()
 * Trail:   window.__bystackDebug.trail()
 */

export type LayoutDebugEvent = {
  readonly t: number;
  readonly seq: number;
  readonly kind: string;
  readonly detail: Record<string, unknown>;
};

export type GroupSnap = {
  readonly urn: string;
  readonly label: string;
  readonly box: { x: number; y: number; w: number; h: number };
  readonly home: { x: number; y: number } | null;
  readonly vel: { x: number; y: number } | null;
  readonly distHome: number;
  readonly nearHome: boolean;
};

export type OverlapSnap = {
  readonly a: string;
  readonly b: string;
  readonly overlapX: number;
  readonly overlapY: number;
  readonly homesAlsoOverlap: boolean;
};

const TRAIL_MAX = 400;
const trail: LayoutDebugEvent[] = [];
let seq = 0;
let lastOverlapSig = '';
let lastStepLogAt = 0;
let lastPushLogAt = 0;
let lastPushSig = '';
let installed = false;

function readEnabled(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    const q = new URLSearchParams(window.location.search).get('debug');
    if (q === '0' || q === 'false') return false;
    if (q === '1' || q === 'true') return true;
    const ls = window.localStorage.getItem('bystack:debug');
    if (ls === '0' || ls === 'false') return false;
    if (ls === '1' || ls === 'true') return true;
  } catch {
    /* ignore */
  }
  // Default on while hunting the stacked-frame mess — turn off with debug=0.
  return true;
}

let enabled = false;

export function isLayoutDebugEnabled(): boolean {
  return enabled;
}

export function shortUrn(urn: string): string {
  const parts = urn.split(/[:/]/).filter(Boolean);
  return parts[parts.length - 1] ?? urn;
}

export function layoutLog(kind: string, detail: Record<string, unknown> = {}): void {
  if (!enabled) return;
  const event: LayoutDebugEvent = {
    t: performance.now(),
    seq: ++seq,
    kind,
    detail,
  };
  trail.push(event);
  if (trail.length > TRAIL_MAX) trail.shift();

  const tag = `%c[bystack:${kind}]`;
  const style = styleFor(kind);
  // eslint-disable-next-line no-console
  console.log(tag, style, detail);
}

function styleFor(kind: string): string {
  if (kind.startsWith('overlap') || kind.startsWith('conflict')) return 'color:#f87171;font-weight:700';
  if (kind.startsWith('seed')) return 'color:#60a5fa;font-weight:700';
  if (kind.startsWith('group')) return 'color:#a78bfa;font-weight:700';
  if (kind.startsWith('card')) return 'color:#34d399;font-weight:700';
  if (kind.startsWith('force') || kind.startsWith('settle')) return 'color:#fbbf24;font-weight:700';
  return 'color:#94a3b8';
}

export function layoutTrail(): readonly LayoutDebugEvent[] {
  return trail.slice();
}

export function clearLayoutTrail(): void {
  trail.length = 0;
  seq = 0;
  lastOverlapSig = '';
}

export type LayoutDebugDump = {
  readonly at: number;
  readonly active: boolean;
  readonly alpha: number;
  readonly settleSteps: number;
  readonly dragging: string | null;
  readonly draggedGroup: string | null;
  readonly editedStack: string | null;
  readonly groups: GroupSnap[];
  readonly overlaps: OverlapSnap[];
  readonly homeConflicts: OverlapSnap[];
  readonly whyMessed: string[];
  readonly recent: LayoutDebugEvent[];
};

export function explainMess(dump: Omit<LayoutDebugDump, 'recent' | 'whyMessed'>): string[] {
  const why: string[] = [];
  if (dump.overlaps.length) {
    why.push(
      `Frames overlap now: ${dump.overlaps.map((o) => `${shortUrn(o.a)}∩${shortUrn(o.b)}`).join(', ')}`,
    );
  }
  if (dump.homeConflicts.length) {
    why.push(
      `HOMES permanently conflict (spring+collision will fight forever): ${dump.homeConflicts
        .map((o) => `${shortUrn(o.a)}∩${shortUrn(o.b)}`)
        .join(', ')}`,
    );
  }
  for (const g of dump.groups) {
    if (g.distHome > 8) {
      why.push(`${g.label} is ${g.distHome.toFixed(0)}px off its home (aspiring / kicked)`);
    }
  }
  if (dump.dragging) why.push(`Card held: ${shortUrn(dump.dragging)}`);
  if (dump.draggedGroup) why.push(`Frame dragged: ${shortUrn(dump.draggedGroup)}`);
  if (dump.editedStack) why.push(`Stack locked by card edit: ${shortUrn(dump.editedStack)}`);
  if (!why.length) why.push('No frame overlap / home conflict detected in this snapshot.');
  return why;
}

export function installLayoutDebugApi(getDump: () => LayoutDebugDump): void {
  if (typeof window === 'undefined') return;
  enabled = readEnabled();
  if (installed) {
    (window as unknown as { __bystackDebug: unknown }).__bystackDebug = api(getDump);
    return;
  }
  installed = true;
  (window as unknown as { __bystackDebug: unknown }).__bystackDebug = api(getDump);
  layoutLog('debug:ready', {
    enabled,
    hint: "dump() / trail() / localStorage bystack:debug='0' to silence",
  });
}

function api(getDump: () => LayoutDebugDump) {
  return {
    enabled: () => enabled,
    setEnabled: (on: boolean) => {
      enabled = on;
      try {
        window.localStorage.setItem('bystack:debug', on ? '1' : '0');
      } catch {
        /* ignore */
      }
      layoutLog('debug:toggle', { enabled: on });
    },
    dump: () => {
      const d = getDump();
      // eslint-disable-next-line no-console
      console.groupCollapsed('%c[bystack:DUMP]', 'color:#f472b6;font-weight:700');
      // eslint-disable-next-line no-console
      console.table(
        d.groups.map((g) => ({
          stack: g.label,
          x: Math.round(g.box.x),
          y: Math.round(g.box.y),
          homeX: g.home ? Math.round(g.home.x) : null,
          homeY: g.home ? Math.round(g.home.y) : null,
          dist: Math.round(g.distHome),
          near: g.nearHome,
        })),
      );
      // eslint-disable-next-line no-console
      console.log('overlaps', d.overlaps);
      // eslint-disable-next-line no-console
      console.log('homeConflicts', d.homeConflicts);
      // eslint-disable-next-line no-console
      console.log('whyMessed', d.whyMessed);
      // eslint-disable-next-line no-console
      console.log('pointer', {
        dragging: d.dragging,
        draggedGroup: d.draggedGroup,
        editedStack: d.editedStack,
        alpha: d.alpha,
        settleSteps: d.settleSteps,
        active: d.active,
      });
      // eslint-disable-next-line no-console
      console.log('recent trail', d.recent);
      // eslint-disable-next-line no-console
      console.groupEnd();
      return d;
    },
    trail: () => layoutTrail(),
    clear: () => clearLayoutTrail(),
  };
}

/** Throttled: log when overlap signature changes, or periodically while overlapping. */
export function maybeLogOverlapState(
  overlaps: OverlapSnap[],
  groups: GroupSnap[],
  ctx: Record<string, unknown>,
): void {
  if (!enabled) return;
  const sig = overlaps
    .map((o) => `${o.a}|${o.b}:${Math.round(o.overlapX)}x${Math.round(o.overlapY)}`)
    .sort()
    .join(';');
  const now = performance.now();
  const changed = sig !== lastOverlapSig;
  if (!overlaps.length) {
    if (lastOverlapSig) {
      layoutLog('overlap:cleared', { ...ctx, previous: lastOverlapSig });
      lastOverlapSig = '';
    }
    return;
  }
  if (changed || now - lastStepLogAt > 500) {
    lastStepLogAt = now;
    lastOverlapSig = sig;
    layoutLog(changed ? 'overlap:changed' : 'overlap:still', {
      ...ctx,
      overlaps: overlaps.map((o) => ({
        pair: `${shortUrn(o.a)}∩${shortUrn(o.b)}`,
        ox: +o.overlapX.toFixed(1),
        oy: +o.overlapY.toFixed(1),
        homesAlsoOverlap: o.homesAlsoOverlap,
      })),
      groups: groups.map((g) => ({
        s: g.label,
        box: [Math.round(g.box.x), Math.round(g.box.y)],
        home: g.home ? [Math.round(g.home.x), Math.round(g.home.y)] : null,
        dist: Math.round(g.distHome),
      })),
    });
  }
}

export function frameOverlap(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
  gap: number,
): { overlapX: number; overlapY: number } | null {
  const acx = a.x + a.width / 2;
  const acy = a.y + a.height / 2;
  const bcx = b.x + b.width / 2;
  const bcy = b.y + b.height / 2;
  const overlapX = (a.width + b.width) / 2 + gap - Math.abs(acx - bcx);
  const overlapY = (a.height + b.height) / 2 + gap - Math.abs(acy - bcy);
  if (overlapX <= 0 || overlapY <= 0) return null;
  return { overlapX, overlapY };
}

/** Throttle collision push spam while stacks keep fighting. */
export function maybeLogPushes(
  prefer: string | null,
  pushes: readonly Record<string, unknown>[],
  after: unknown,
): void {
  if (!enabled || !pushes.length) return;
  const sig = `${prefer ?? '-'}|${pushes.map((p) => `${p.winner}>${p.loser}`).join(',')}`;
  const now = performance.now();
  if (sig === lastPushSig && now - lastPushLogAt < 350) return;
  lastPushSig = sig;
  lastPushLogAt = now;
  layoutLog('collision:push', { prefer, pushes, after });
}
