/**
 * Interactive topology — fixed ELK homes + playful drag + spring-home release.
 *
 * Cards inside a stack: drag = play, release = spring home.
 * Free floaters: drag and stay where dropped.
 * Whole stack frame: translate preserves relative layout and stays put.
 */

import type { GraphNode, NodeKind, Urn } from '../../../api/types';
import type { Point } from './elkLayout';
import { sizeOf } from './geometry';
import {
  explainMess,
  frameOverlap,
  installLayoutDebugApi,
  layoutLog,
  layoutTrail,
  maybeLogOverlapState,
  maybeLogPushes,
  shortUrn,
  type GroupSnap,
  type LayoutDebugDump,
  type OverlapSnap,
} from './layoutDebug';

export interface GroupBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

const INSET = 10;
const GAP = 14;
/** Minimum clear space between stack frames. */
const GROUP_GAP = 24;
const PUSH_ITERS = 8;
/**
 * Fraction of a frame overlap that a *live* separation pass resolves per call.
 *
 * Clearing a big one whole in a single pass is what made a neighbouring stack
 * appear somewhere else the instant you leaned on it — the correction was
 * instant while everything else on the canvas is sprung.
 *
 * Only the interactive passes ease. Seeding and `forceSeat` still resolve hard:
 * nothing is being watched in those moments, and they are the guarantee that
 * separation terminates.
 */
const GROUP_PUSH_EASE = 0.18;
/**
 * An eased push is never weaker than this many px per pass.
 *
 * A frame shoved off its seat springs back at up to MAX_SPEED per frame, so a
 * push softer than that finds an equilibrium with the spring and the pair sits
 * permanently overlapping — smooth, but no longer separated. Applying anything
 * under the floor whole keeps "frames never overlap" true through an ordinary
 * drag, where each pointer move only creates a few px of overlap anyway, and
 * leaves the easing to do its actual job: the jumps large enough to read as a
 * teleport.
 */
const GROUP_PUSH_FLOOR_PX = 36;

/**
 * Soft settle — underdamped on purpose. The card overshoots its seat by a
 * hair and rocks back, which is what makes releasing one feel satisfying
 * rather than magnetic.
 */
const STIFFNESS = 0.16;
const DAMPING = 0.78;
/** Groups are heavier than cards, but still allowed a soft overshoot. */
const GROUP_STIFFNESS = 0.12;
const GROUP_DAMPING = 0.82;
const ALPHA_DECAY = 0.96;
const ALPHA_MIN = 0.02;
const MAX_SPEED = 32;
const ENTRANCE_JITTER = 40;
/** How much of the pointer's parting speed a flung card keeps. */
const FLING_SCALE = 0.7;
const MAX_FLING = 28;
/**
 * Alpha is the wobble budget; arrival is decided by distance instead, so a
 * bouncy release still lands exactly on its seat. The step cap is the escape
 * hatch for a pair that can never satisfy both spring and separation.
 */
const REST_EPSILON = 0.35;
const GROUP_REST = 1.2;
/** Snap only at the end of the rock — a large SNAP killed overshoot. */
const SNAP = 0.55;
const SNAP_VEL = 0.35;
const PULL_FLOOR = 0.28;
const MAX_SETTLE_STEPS = 600;
const CARD_KICK = 16;
/**
 * Small on purpose. A stack frame is a big object and an impulse this size
 * reads as a shove; now that separation itself slides, the kick only has to
 * signal "you're crowding me" and let the eased push do the moving.
 */
const GROUP_KICK = 5;

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  pinned: boolean;
}

interface Body {
  urn: Urn;
  kind: NodeKind;
  x: number;
  y: number;
  hw: number;
  hh: number;
}

export class InteractiveLayout {
  private particles = new Map<Urn, Particle>();
  /** Permanent seats — updated for free floaters / group moves. */
  private homes = new Map<Urn, Point>();
  private groupBounds = new Map<Urn, GroupBox>();
  /** Rest seat for each stack frame — siblings spring back here after a kick. */
  private groupHomes = new Map<Urn, Point>();
  private groupVel = new Map<Urn, Point>();
  /** Monotonic seat generation — newer user home wins when seats collide. */
  private groupHomeSeq = new Map<Urn, number>();
  private homeClock = 0;
  private edgePaths = new Map<string, Point[]>();
  private parentOf = new Map<Urn, Urn>();
  private membersOf = new Map<Urn, Urn[]>();
  private kinds = new Map<Urn, NodeKind>();
  private dragging = false;
  private draggedUrn: Urn | null = null;
  /** Stack frame currently under the pointer (wobble siblings like cards). */
  private draggedGroup: Urn | null = null;
  private alpha = 0;
  private settleSteps = 0;
  private positionCache = new Map<Urn, Point>();
  /** After a group is relocated, ELK polylines no longer match. */
  private pathsStale = false;
  private debugInstalled = false;

  /** Structured dump for console / window.__bystackDebug.dump(). */
  debugDump(): LayoutDebugDump {
    const groups = this.snapshotGroups();
    const overlaps = this.snapshotOverlaps(false);
    const homeConflicts = this.snapshotOverlaps(true);
    const editedStack =
      this.dragging && this.draggedUrn ? (this.parentOf.get(this.draggedUrn) ?? null) : null;
    const base = {
      at: performance.now(),
      active: this.active,
      alpha: this.alpha,
      settleSteps: this.settleSteps,
      dragging: this.draggedUrn,
      draggedGroup: this.draggedGroup,
      editedStack,
      groups,
      overlaps,
      homeConflicts,
    };
    return {
      ...base,
      whyMessed: explainMess(base),
      recent: layoutTrail().slice(-40),
    };
  }

  private ensureDebugApi(): void {
    if (this.debugInstalled) return;
    this.debugInstalled = true;
    installLayoutDebugApi(() => this.debugDump());
  }

  private snapshotGroups(): GroupSnap[] {
    const out: GroupSnap[] = [];
    for (const [urn, box] of this.groupBounds) {
      const home = this.groupHomes.get(urn) ?? null;
      const vel = this.groupVel.get(urn) ?? null;
      const distHome = home ? Math.hypot(box.x - home.x, box.y - home.y) : 0;
      out.push({
        urn,
        label: shortUrn(urn),
        box: { x: box.x, y: box.y, w: box.width, h: box.height },
        home: home ? { x: home.x, y: home.y } : null,
        vel: vel ? { x: vel.x, y: vel.y } : null,
        distHome,
        nearHome: distHome <= 8,
      });
    }
    return out;
  }

  private snapshotOverlaps(homes: boolean): OverlapSnap[] {
    const ids = [...this.groupBounds.keys()];
    const out: OverlapSnap[] = [];
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const aUrn = ids[i];
        const bUrn = ids[j];
        let a = this.groupBounds.get(aUrn);
        let b = this.groupBounds.get(bUrn);
        if (!a || !b) continue;
        if (homes) {
          const ah = this.groupHomes.get(aUrn);
          const bh = this.groupHomes.get(bUrn);
          if (!ah || !bh) continue;
          a = { x: ah.x, y: ah.y, width: a.width, height: a.height };
          b = { x: bh.x, y: bh.y, width: b.width, height: b.height };
        }
        const hit = frameOverlap(a, b, GROUP_GAP);
        if (!hit) continue;
        const ah = this.groupHomes.get(aUrn);
        const bh = this.groupHomes.get(bUrn);
        let homesAlsoOverlap = false;
        if (ah && bh) {
          const aBox = this.groupBounds.get(aUrn)!;
          const bBox = this.groupBounds.get(bUrn)!;
          homesAlsoOverlap = !!frameOverlap(
            { x: ah.x, y: ah.y, width: aBox.width, height: aBox.height },
            { x: bh.x, y: bh.y, width: bBox.width, height: bBox.height },
            GROUP_GAP,
          );
        }
        out.push({
          a: aUrn,
          b: bUrn,
          overlapX: hit.overlapX,
          overlapY: hit.overlapY,
          homesAlsoOverlap,
        });
      }
    }
    return out;
  }

  get active(): boolean {
    if (this.dragging || this.draggedGroup) return true;
    if (this.settleSteps > MAX_SETTLE_STEPS) return false;
    return this.alpha > ALPHA_MIN || !this.seated();
  }

  /** Everyone on their seat and holding still — cards and stack frames. */
  private seated(): boolean {
    for (const [urn, p] of this.particles) {
      const home = this.homes.get(urn);
      if (!home) continue;
      if (Math.abs(p.x - home.x) > REST_EPSILON) return false;
      if (Math.abs(p.y - home.y) > REST_EPSILON) return false;
      if (Math.abs(p.vx) > REST_EPSILON || Math.abs(p.vy) > REST_EPSILON) return false;
    }
    for (const [urn, box] of this.groupBounds) {
      const home = this.groupHomes.get(urn);
      const vel = this.groupVel.get(urn);
      if (!home) continue;
      if (Math.abs(box.x - home.x) > GROUP_REST) return false;
      if (Math.abs(box.y - home.y) > GROUP_REST) return false;
      if (vel && (Math.abs(vel.x) > REST_EPSILON || Math.abs(vel.y) > REST_EPSILON)) return false;
    }
    return true;
  }

  /** The card currently held, for the lift + home-ghost cues. */
  get held(): Urn | null {
    return this.draggedUrn;
  }

  /** Permanent seats — what the renderer draws the ghost outline at. */
  homesMap(): ReadonlyMap<Urn, Point> {
    return this.homes;
  }

  positionsMap(): ReadonlyMap<Urn, Point> {
    this.positionCache.clear();
    for (const [urn, p] of this.particles) {
      this.positionCache.set(urn, { x: p.x, y: p.y });
    }
    return this.positionCache;
  }

  groups(): ReadonlyMap<Urn, GroupBox> {
    return this.groupBounds;
  }

  paths(): ReadonlyMap<string, Point[]> {
    // Live routes while playing / after a group move.
    if (this.active || this.pathsStale) return new Map();
    return this.edgePaths;
  }

  bounds(): { minX: number; minY: number; maxX: number; maxY: number } | null {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    for (const [urn, pos] of this.particles) {
      const kind = this.kinds.get(urn);
      const [hw, hh] = kind ? sizeOf(kind) : [100, 44];
      minX = Math.min(minX, pos.x - hw);
      minY = Math.min(minY, pos.y - hh);
      maxX = Math.max(maxX, pos.x + hw);
      maxY = Math.max(maxY, pos.y + hh);
    }
    for (const box of this.groupBounds.values()) {
      minX = Math.min(minX, box.x);
      minY = Math.min(minY, box.y);
      maxX = Math.max(maxX, box.x + box.width);
      maxY = Math.max(maxY, box.y + box.height);
    }
    return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
  }

  /** Seed permanent homes from ELK. */
  seed(
    positions: ReadonlyMap<Urn, Point>,
    groupBounds: ReadonlyMap<Urn, GroupBox>,
    edgePaths: ReadonlyMap<string, Point[]>,
    stackMembers: ReadonlyMap<Urn, readonly Urn[]>,
    nodes: readonly GraphNode[],
  ): void {
    this.ensureDebugApi();
    const beforeHomes = [...this.groupHomes.entries()].map(([urn, p]) => ({
      s: shortUrn(urn),
      home: [Math.round(p.x), Math.round(p.y)],
    }));

    this.groupBounds = new Map(
      [...groupBounds.entries()].map(([urn, box]) => [urn, { ...box }]),
    );
    this.edgePaths = new Map(edgePaths);
    this.dragging = false;
    this.draggedUrn = null;
    this.draggedGroup = null;
    this.settleSteps = 0;
    this.pathsStale = false;

    // Keep user-placed stack seats across ELK reseeds — resetting them every
    // graph tick was a major source of flicker and "lost" placements.
    const keptGroupHomes = new Map(this.groupHomes);
    const keptHomeSeq = new Map(this.groupHomeSeq);
    this.groupHomes.clear();
    this.groupHomeSeq.clear();
    this.groupVel.clear();
    for (const [urn, box] of this.groupBounds) {
      const kept = keptGroupHomes.get(urn);
      if (kept) {
        this.groupHomes.set(urn, { ...kept });
        this.groupHomeSeq.set(urn, keptHomeSeq.get(urn) ?? 0);
      } else {
        this.setGroupHome(urn, { x: box.x, y: box.y }, false);
      }
      this.groupVel.set(urn, { x: 0, y: 0 });
    }

    this.parentOf.clear();
    this.membersOf.clear();
    this.kinds.clear();
    for (const node of nodes) this.kinds.set(node.urn, node.kind);

    for (const [stack, members] of stackMembers) {
      this.membersOf.set(stack, [...members]);
      for (const m of members) this.parentOf.set(m, stack);
    }

    const live = new Set(positions.keys());
    for (const urn of [...this.particles.keys()]) {
      if (!live.has(urn)) this.particles.delete(urn);
    }
    this.homes.clear();

    let heated = false;
    for (const [urn, home] of positions) {
      const existing = this.particles.get(urn);
      if (existing) {
        const jumped = Math.hypot(existing.x - home.x, existing.y - home.y) > 48;
        this.homes.set(urn, { ...home });
        existing.pinned = false;
        if (jumped) {
          existing.vx = 0;
          existing.vy = 0;
          heated = true;
        } else if (Math.hypot(existing.x - home.x, existing.y - home.y) <= SNAP) {
          // Already on seat — pin exactly, do not reheat the whole canvas.
          existing.x = home.x;
          existing.y = home.y;
          existing.vx = 0;
          existing.vy = 0;
        } else {
          heated = true;
        }
        continue;
      }

      this.homes.set(urn, { ...home });
      this.particles.set(urn, {
        x: home.x + (Math.random() - 0.5) * ENTRANCE_JITTER,
        y: home.y + (Math.random() - 0.5) * ENTRANCE_JITTER,
        vx: (Math.random() - 0.5) * 6,
        vy: (Math.random() - 0.5) * 6,
        pinned: false,
      });
      heated = true;
    }

    if (heated) this.alpha = 1;

    // Re-apply kept stack translations onto the fresh ELK member seats.
    for (const [urn, box] of this.groupBounds) {
      const home = this.groupHomes.get(urn);
      if (!home) continue;
      const dx = home.x - box.x;
      const dy = home.y - box.y;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
      this.shiftGroup(urn, dx, dy);
      // shiftGroup moved homes too — restore the kept seat.
      this.groupHomes.set(urn, { ...home });
    }

    for (const stack of this.membersOf.keys()) this.resolveGroup(stack);

    // ELK / restored user seats must not claim the same rectangle — otherwise
    // forceSeat freezes the mess in the screenshot (FRONTEND∩BACKEND).
    const homeFixes = this.ensureHomesSeparated();
    if (homeFixes > 0) this.resolveGroupFrames();

    const seeded = [...this.groupBounds.entries()].map(([urn, box]) => {
      const home = this.groupHomes.get(urn)!;
      return {
        s: shortUrn(urn),
        box: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
        home: [Math.round(home.x), Math.round(home.y)],
        keptUserHome: beforeHomes.some((h) => h.s === shortUrn(urn)),
      };
    });
    layoutLog('seed', {
      heated,
      homeFixes,
      stacks: seeded,
      previousHomes: beforeHomes,
      homeConflicts: this.snapshotOverlaps(true).map((o) => ({
        pair: `${shortUrn(o.a)}∩${shortUrn(o.b)}`,
        ox: +o.overlapX.toFixed(1),
        oy: +o.overlapY.toFixed(1),
      })),
      frameOverlaps: this.snapshotOverlaps(false).map((o) => ({
        pair: `${shortUrn(o.a)}∩${shortUrn(o.b)}`,
      })),
      why: explainMess({
        at: performance.now(),
        active: this.active,
        alpha: this.alpha,
        settleSteps: this.settleSteps,
        dragging: null,
        draggedGroup: null,
        editedStack: null,
        groups: this.snapshotGroups(),
        overlaps: this.snapshotOverlaps(false),
        homeConflicts: this.snapshotOverlaps(true),
      }),
    });
  }

  step(): void {
    if (!this.active) return;

    // While dragging, keep a mild floor so siblings react. After release the
    // floor is what finishes the journey once the wobble budget has burnt off.
    const pull =
      this.dragging || this.draggedGroup
        ? Math.max(this.alpha, 0.4)
        : Math.max(this.alpha, PULL_FLOOR);
    if (this.dragging || this.draggedGroup) this.settleSteps = 0;
    else this.settleSteps += 1;

    for (const [urn, p] of this.particles) {
      if (p.pinned) {
        p.vx = 0;
        p.vy = 0;
        continue;
      }
      const home = this.homes.get(urn);
      if (!home) continue;

      const dist = Math.hypot(home.x - p.x, home.y - p.y);
      if (dist <= SNAP && Math.hypot(p.vx, p.vy) < SNAP_VEL) {
        p.x = home.x;
        p.y = home.y;
        p.vx = 0;
        p.vy = 0;
        continue;
      }

      p.vx += (home.x - p.x) * STIFFNESS * pull;
      p.vy += (home.y - p.y) * STIFFNESS * pull;
      p.vx *= DAMPING;
      p.vy *= DAMPING;
      p.x += clamp(p.vx, -MAX_SPEED, MAX_SPEED);
      p.y += clamp(p.vy, -MAX_SPEED, MAX_SPEED);
    }

    // Stack frames spring home — heavier damping so they do not chatter
    // against collision resolution every frame.
    const editedStack =
      this.dragging && this.draggedUrn ? (this.parentOf.get(this.draggedUrn) ?? null) : null;

    for (const [urn, box] of this.groupBounds) {
      if (urn === this.draggedGroup || urn === editedStack) {
        // Frame under the pointer (or holding a played card) stays put —
        // waiters yield to it instead of sliding into the edit.
        const vel = this.groupVel.get(urn);
        if (vel) {
          vel.x = 0;
          vel.y = 0;
        }
        continue;
      }
      const home = this.groupHomes.get(urn);
      let vel = this.groupVel.get(urn);
      if (!home) continue;
      if (!vel) {
        vel = { x: 0, y: 0 };
        this.groupVel.set(urn, vel);
      }

      const dist = Math.hypot(home.x - box.x, home.y - box.y);
      if (dist <= SNAP && Math.hypot(vel.x, vel.y) < SNAP_VEL) {
        this.shiftGroup(urn, home.x - box.x, home.y - box.y);
        vel.x = 0;
        vel.y = 0;
        continue;
      }

      vel.x += (home.x - box.x) * GROUP_STIFFNESS * pull;
      vel.y += (home.y - box.y) * GROUP_STIFFNESS * pull;
      vel.x *= GROUP_DAMPING;
      vel.y *= GROUP_DAMPING;
      const dx = clamp(vel.x, -MAX_SPEED, MAX_SPEED);
      const dy = clamp(vel.y, -MAX_SPEED, MAX_SPEED);
      if (Math.abs(dx) > 1e-4 || Math.abs(dy) > 1e-4) this.shiftGroup(urn, dx, dy);
    }

    // Hard collision only while playing. During snap-home it traps nodes that
    // sit between someone else and their seat — they jam and never arrive.
    // While a card inside a stack is held, that stack wins frame collisions so
    // an aspiring neighbour cannot slip into the assembly being edited.
    if (this.dragging) {
      for (const stack of this.membersOf.keys()) this.resolveGroup(stack);
      this.resolveFree();
      if (editedStack || this.groupsOverlap()) {
        this.resolveGroupFrames(editedStack ?? undefined, GROUP_PUSH_EASE);
      }
    } else if (this.draggedGroup) {
      // Kick is applied in translateGroup (once per pointer move), not here —
      // re-kicking every animation frame made stacks vibrate.
      this.resolveGroupFrames(this.draggedGroup, GROUP_PUSH_EASE);
    } else if (this.groupsOverlap()) {
      this.resolveGroupFrames(undefined, GROUP_PUSH_EASE);
    }

    if (this.alpha < 0.2 && !this.dragging && !this.draggedGroup) {
      for (const stack of this.membersOf.keys()) this.resolveGroupSoft(stack);
    }

    if (this.settleSteps > MAX_SETTLE_STEPS) this.forceSeat();

    if (!this.dragging && !this.draggedGroup) this.alpha *= ALPHA_DECAY;

    maybeLogOverlapState(this.snapshotOverlaps(false), this.snapshotGroups(), {
      phase: this.dragging ? 'card-drag' : this.draggedGroup ? 'group-drag' : 'settle',
      prefer: editedStack ?? this.draggedGroup,
      alpha: +this.alpha.toFixed(3),
      settleSteps: this.settleSteps,
    });
  }

  /** Temporary play — homes stay put. */
  move(urn: Urn, at: Point): void {
    const p = this.particles.get(urn);
    if (!p) return;

    const parent = this.parentOf.get(urn) ?? null;
    const first = !this.dragging || this.draggedUrn !== urn;
    p.x = at.x;
    p.y = at.y;
    p.vx = 0;
    p.vy = 0;
    p.pinned = true;
    this.dragging = true;
    this.draggedUrn = urn;
    this.alpha = Math.max(this.alpha, 0.7);

    if (parent) {
      this.clampParticle(urn, parent);
      this.kickSiblings(parent, urn);
      this.resolveGroup(parent, urn);
    } else {
      this.resolveFree(urn);
    }

    if (first) {
      layoutLog('card:grab', {
        card: shortUrn(urn),
        stack: parent ? shortUrn(parent) : null,
        at: [Math.round(at.x), Math.round(at.y)],
        stackLocked: parent ? shortUrn(parent) : null,
        overlapsBeforeYield: this.snapshotOverlaps(false).map(
          (o) => `${shortUrn(o.a)}∩${shortUrn(o.b)}`,
        ),
      });
    }
  }

  /**
   * Let go — grouped cards spring home; free floaters stay where dropped.
   * A parting velocity lets a grouped card carry the throw before the spring.
   */
  release(urn: Urn, velocity?: Point): void {
    const p = this.particles.get(urn);
    const parent = this.parentOf.get(urn);

    layoutLog('card:release', {
      card: shortUrn(urn),
      stack: parent ? shortUrn(parent) : null,
      fling: velocity
        ? [+velocity.x.toFixed(1), +velocity.y.toFixed(1)]
        : null,
      freeStay: !parent,
      pos: p ? [Math.round(p.x), Math.round(p.y)] : null,
    });

    if (p && !parent) {
      this.homes.set(urn, { x: p.x, y: p.y });
      p.pinned = false;
      p.vx = 0;
      p.vy = 0;
    } else if (p) {
      p.pinned = false;
      p.vx = clamp((velocity?.x ?? 0) * FLING_SCALE, -MAX_FLING, MAX_FLING);
      p.vy = clamp((velocity?.y ?? 0) * FLING_SCALE, -MAX_FLING, MAX_FLING);
    }

    // Soften leftover kick — keep enough energy for a sibling rock.
    for (const [otherUrn, other] of this.particles) {
      if (otherUrn !== urn && !other.pinned) {
        other.vx *= 0.55;
        other.vy *= 0.55;
      }
    }
    this.dragging = false;
    this.draggedUrn = null;
    this.settleSteps = 0;
    this.alpha = parent ? 1 : Math.min(this.alpha, 0.3);
  }

  /**
   * Drag a stack frame — follows the pointer; siblings kick and yield.
   * Dropping redefines THIS frame's home; others keep theirs and try to return.
   */
  translateGroup(stackUrn: Urn, dx: number, dy: number): void {
    if (!this.groupBounds.has(stackUrn)) return;
    const first = this.draggedGroup !== stackUrn;
    this.draggedGroup = stackUrn;
    this.alpha = Math.max(this.alpha, 0.7);
    this.shiftGroup(stackUrn, dx, dy);
    const vel = this.groupVel.get(stackUrn);
    if (vel) {
      vel.x = 0;
      vel.y = 0;
    }
    this.kickGroupSiblings(stackUrn);
    this.resolveGroupFrames(stackUrn, GROUP_PUSH_EASE);
    this.pathsStale = true;

    if (first) {
      const box = this.groupBounds.get(stackUrn)!;
      const home = this.groupHomes.get(stackUrn);
      layoutLog('group:grab', {
        stack: shortUrn(stackUrn),
        box: [Math.round(box.x), Math.round(box.y)],
        oldHome: home ? [Math.round(home.x), Math.round(home.y)] : null,
        note: 'drop will redefine THIS stack home; others keep theirs',
      });
    }
  }

  /**
   * Let go — the dragged stack's drop becomes its new home (user redefined it).
   * Kicked siblings keep their original homes and keep pulling toward them.
   */
  releaseGroup(stackUrn: Urn): void {
    const box = this.groupBounds.get(stackUrn);
    const prevHome = this.groupHomes.get(stackUrn);
    this.draggedGroup = null;

    // Drop wins live collisions, then the new seat is parked in a clear slot
    // (nearest legal home to the drop — never permanently overlaps another seat).
    this.resolveGroupFrames(stackUrn, GROUP_PUSH_EASE);
    if (box) this.setGroupHome(stackUrn, { x: box.x, y: box.y }, true);
    const parked = this.parkHomeClearOfOthers(stackUrn);

    for (const [urn, vel] of this.groupVel) {
      if (urn !== stackUrn) {
        vel.x *= 0.35;
        vel.y *= 0.35;
      }
    }
    this.settleSteps = 0;
    this.alpha = 1;
    this.pathsStale = true;

    const after = this.groupBounds.get(stackUrn);
    layoutLog('group:release', {
      stack: shortUrn(stackUrn),
      newHome: after ? [Math.round(after.x), Math.round(after.y)] : null,
      prevHome: prevHome ? [Math.round(prevHome.x), Math.round(prevHome.y)] : null,
      parkedClearOfOthers: parked,
      homeConflictsNow: this.snapshotOverlaps(true).map((o) => ({
        pair: `${shortUrn(o.a)}∩${shortUrn(o.b)}`,
        ox: +o.overlapX.toFixed(1),
        oy: +o.overlapY.toFixed(1),
      })),
      why: explainMess({
        at: performance.now(),
        active: true,
        alpha: this.alpha,
        settleSteps: this.settleSteps,
        dragging: null,
        draggedGroup: null,
        editedStack: null,
        groups: this.snapshotGroups(),
        overlaps: this.snapshotOverlaps(false),
        homeConflicts: this.snapshotOverlaps(true),
      }),
    });
  }

  /** Click without drag — abort group play without redefining its seat. */
  cancelGroupDrag(stackUrn: Urn): void {
    if (this.draggedGroup !== stackUrn) {
      layoutLog('group:cancel-noop', {
        stack: shortUrn(stackUrn),
        reason: 'never started translateGroup (click under threshold)',
      });
      return;
    }
    this.draggedGroup = null;
    const vel = this.groupVel.get(stackUrn);
    if (vel) {
      vel.x = 0;
      vel.y = 0;
    }
    this.settleSteps = 0;
    this.alpha = Math.max(this.alpha, 0.5);
    layoutLog('group:cancel', {
      stack: shortUrn(stackUrn),
      note: 'home NOT redefined',
    });
  }

  private forceSeat(): void {
    const before = this.snapshotGroups();
    // Never snap into overlapping homes — that froze FRONTEND∩BACKEND.
    const homeFixes = this.ensureHomesSeparated();
    this.resolveGroupFrames();

    for (const [urn, p] of this.particles) {
      const home = this.homes.get(urn);
      if (!home || p.pinned) continue;
      p.x = home.x;
      p.y = home.y;
      p.vx = 0;
      p.vy = 0;
    }
    for (const [urn, box] of this.groupBounds) {
      const home = this.groupHomes.get(urn);
      const vel = this.groupVel.get(urn);
      if (!home) continue;
      this.shiftGroup(urn, home.x - box.x, home.y - box.y);
      if (vel) {
        vel.x = 0;
        vel.y = 0;
      }
    }
    this.resolveGroupFrames();
    // Sync homes to final non-overlapping live seats (no seq bump — settle).
    for (const [urn, box] of this.groupBounds) {
      this.setGroupHome(urn, { x: box.x, y: box.y }, false);
    }

    this.alpha = 0;
    this.settleSteps = MAX_SETTLE_STEPS + 1;
    layoutLog('forceSeat', {
      reason: `settleSteps exceeded ${MAX_SETTLE_STEPS} — clear homes, snap, freeze`,
      homeFixes,
      before: before.map((g) => ({ s: g.label, dist: Math.round(g.distHome) })),
      after: this.snapshotGroups().map((g) => ({
        s: g.label,
        box: [Math.round(g.box.x), Math.round(g.box.y)],
        home: g.home ? [Math.round(g.home.x), Math.round(g.home.y)] : null,
      })),
      stillOverlapping: this.snapshotOverlaps(false).map((o) => `${shortUrn(o.a)}∩${shortUrn(o.b)}`),
      homeConflicts: this.snapshotOverlaps(true).map((o) => `${shortUrn(o.a)}∩${shortUrn(o.b)}`),
    });
  }

  private setGroupHome(urn: Urn, at: Point, bumpSeq: boolean): void {
    this.groupHomes.set(urn, { x: at.x, y: at.y });
    if (bumpSeq) this.groupHomeSeq.set(urn, ++this.homeClock);
    else if (!this.groupHomeSeq.has(urn)) this.groupHomeSeq.set(urn, 0);
  }

  /**
   * Push stack homes apart until no two seats claim the same rectangle.
   * Newer seats win; optional prefer wins over everyone.
   * Returns how many separation pushes ran.
   */
  private ensureHomesSeparated(prefer?: Urn): number {
    const ids = [...this.groupBounds.keys()];
    let pushes = 0;
    for (let iter = 0; iter < PUSH_ITERS * 3; iter += 1) {
      let hit = false;
      for (let i = 0; i < ids.length; i += 1) {
        for (let j = i + 1; j < ids.length; j += 1) {
          const aUrn = ids[i];
          const bUrn = ids[j];
          const aSize = this.groupBounds.get(aUrn);
          const bSize = this.groupBounds.get(bUrn);
          const aHome = this.groupHomes.get(aUrn);
          const bHome = this.groupHomes.get(bUrn);
          if (!aSize || !bSize || !aHome || !bHome) continue;
          const a = { x: aHome.x, y: aHome.y, width: aSize.width, height: aSize.height };
          const b = { x: bHome.x, y: bHome.y, width: bSize.width, height: bSize.height };
          const sep = separateFrames(a, b, GROUP_GAP);
          if (!sep) continue;
          hit = true;
          pushes += 1;

          const winner = this.frameWinner(aUrn, bUrn, prefer);
          if (winner === aUrn) {
            this.setGroupHome(bUrn, { x: bHome.x - sep.dx, y: bHome.y - sep.dy }, false);
          } else if (winner === bUrn) {
            this.setGroupHome(aUrn, { x: aHome.x + sep.dx, y: aHome.y + sep.dy }, false);
          } else {
            this.setGroupHome(aUrn, { x: aHome.x + sep.dx / 2, y: aHome.y + sep.dy / 2 }, false);
            this.setGroupHome(bUrn, { x: bHome.x - sep.dx / 2, y: bHome.y - sep.dy / 2 }, false);
          }
        }
      }
      if (!hit) break;
    }
    if (pushes) {
      layoutLog('homes:separated', {
        prefer: prefer ? shortUrn(prefer) : null,
        pushes,
        homes: [...this.groupHomes.entries()].map(([urn, h]) => ({
          s: shortUrn(urn),
          home: [Math.round(h.x), Math.round(h.y)],
          seq: this.groupHomeSeq.get(urn) ?? 0,
        })),
      });
    }
    return pushes;
  }

  /**
   * Find THIS stack a seat that clears every other home, starting from where it
   * was dropped. Used on user drop so the redefined home is always a legal
   * exclusive seat.
   *
   * Only the seat moves. Shoving the live frame here as well was the jolt on
   * release: you let go and the stack teleported out from under the pointer,
   * mid-gesture. Leaving the frame at the drop and moving only its home hands
   * the correction to the group spring, which plays the same distance as
   * motion — the stack slides into its slot instead of appearing in it.
   */
  private parkHomeClearOfOthers(urn: Urn): boolean {
    const box = this.groupBounds.get(urn);
    if (!box) return false;
    const seat = { ...(this.groupHomes.get(urn) ?? { x: box.x, y: box.y }) };
    let moved = false;
    for (let iter = 0; iter < PUSH_ITERS * 3; iter += 1) {
      let hit = false;
      for (const [other, otherBox] of this.groupBounds) {
        if (other === urn) continue;
        const otherHome = this.groupHomes.get(other);
        if (!otherHome) continue;
        const a = { x: seat.x, y: seat.y, width: box.width, height: box.height };
        const b = {
          x: otherHome.x,
          y: otherHome.y,
          width: otherBox.width,
          height: otherBox.height,
        };
        const sep = separateFrames(a, b, GROUP_GAP);
        if (!sep) continue;
        hit = true;
        moved = true;
        seat.x += sep.dx;
        seat.y += sep.dy;
      }
      if (!hit) break;
    }
    if (moved) this.setGroupHome(urn, seat, true);
    return moved;
  }

  private frameWinner(aUrn: Urn, bUrn: Urn, prefer?: Urn): Urn | null {
    if (prefer === aUrn) return aUrn;
    if (prefer === bUrn) return bUrn;
    const aHome = this.groupNearHome(aUrn);
    const bHome = this.groupNearHome(bUrn);
    if (aHome && !bHome) return aUrn;
    if (bHome && !aHome) return bUrn;
    // Both claiming / both away — newer user seat wins so a drop is sticky.
    const sa = this.groupHomeSeq.get(aUrn) ?? 0;
    const sb = this.groupHomeSeq.get(bUrn) ?? 0;
    if (sa > sb) return aUrn;
    if (sb > sa) return bUrn;
    return null;
  }

  private groupsOverlap(): boolean {
    const ids = [...this.groupBounds.keys()];
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = this.groupBounds.get(ids[i]);
        const b = this.groupBounds.get(ids[j]);
        if (!a || !b) continue;
        if (separateFrames(a, b, GROUP_GAP)) return true;
      }
    }
    return false;
  }

  private shiftGroup(stackUrn: Urn, dx: number, dy: number): void {
    const box = this.groupBounds.get(stackUrn);
    if (!box) return;
    box.x += dx;
    box.y += dy;

    for (const m of this.membersOf.get(stackUrn) ?? []) {
      const p = this.particles.get(m);
      const home = this.homes.get(m);
      if (p) {
        p.x += dx;
        p.y += dy;
        p.vx = 0;
        p.vy = 0;
      }
      if (home) {
        home.x += dx;
        home.y += dy;
      }
    }
  }

  private groupNearHome(urn: Urn): boolean {
    const box = this.groupBounds.get(urn);
    const home = this.groupHomes.get(urn);
    if (!box || !home) return false;
    return Math.hypot(box.x - home.x, box.y - home.y) <= 8;
  }

  /** Kick overlapping stack frames — same impulse flavour as card siblings. */
  private kickGroupSiblings(moved: Urn): void {
    const a = this.groupBounds.get(moved);
    if (!a) return;
    const acx = a.x + a.width / 2;
    const acy = a.y + a.height / 2;

    for (const [urn, b] of this.groupBounds) {
      if (urn === moved) continue;
      const overlapX = (a.width + b.width) / 2 + GROUP_GAP - Math.abs(acx - (b.x + b.width / 2));
      const overlapY = (a.height + b.height) / 2 + GROUP_GAP - Math.abs(acy - (b.y + b.height / 2));
      if (overlapX <= 0 || overlapY <= 0) continue;

      const dx = b.x + b.width / 2 - acx || 0.01;
      const dy = b.y + b.height / 2 - acy || 0.01;
      const len = Math.hypot(dx, dy) || 1;
      let vel = this.groupVel.get(urn);
      if (!vel) {
        vel = { x: 0, y: 0 };
        this.groupVel.set(urn, vel);
      }
      vel.x += (dx / len) * GROUP_KICK;
      vel.y += (dy / len) * GROUP_KICK;
    }
  }

  /**
   * Hard separation. Dragged frame wins; otherwise a frame already on its seat
   * wins so waiters stay aside until the path home opens.
   */
  private resolveGroupFrames(prefer?: Urn, ease = 1): void {
    const ids = [...this.groupBounds.keys()];
    const pushes: {
      loser: string;
      winner: string;
      rule: string;
      dx: number;
      dy: number;
    }[] = [];

    // An eased pass sweeps once and lets the next animation frame carry the
    // rest; iterating here as well would resolve ~79% per call and be hard
    // separation wearing a costume.
    const iters = ease >= 1 ? PUSH_ITERS : 1;

    for (let iter = 0; iter < iters; iter += 1) {
      for (let i = 0; i < ids.length; i += 1) {
        for (let j = i + 1; j < ids.length; j += 1) {
          const aUrn = ids[i];
          const bUrn = ids[j];
          const a = this.groupBounds.get(aUrn);
          const b = this.groupBounds.get(bUrn);
          if (!a || !b) continue;
          const sep = separateFrames(a, b, GROUP_GAP);
          if (!sep) continue;
          const push = ease >= 1 ? sep : easePush(sep, ease);

          let winner: Urn | null = this.frameWinner(aUrn, bUrn, prefer);
          let rule = 'split';
          if (prefer === aUrn || prefer === bUrn) rule = 'prefer-arg';
          else if (winner === aUrn || winner === bUrn) {
            const aHome = this.groupNearHome(aUrn);
            const bHome = this.groupNearHome(bUrn);
            if (aHome && !bHome) rule = 'a-near-home';
            else if (bHome && !aHome) rule = 'b-near-home';
            else rule = 'newer-home-seq';
          }

          if (winner === aUrn) {
            this.shiftGroup(bUrn, -push.dx, -push.dy);
            if (pushes.length < 12) {
              pushes.push({
                loser: shortUrn(bUrn),
                winner: shortUrn(aUrn),
                rule,
                dx: +-push.dx.toFixed(1),
                dy: +-push.dy.toFixed(1),
              });
            }
          } else if (winner === bUrn) {
            this.shiftGroup(aUrn, push.dx, push.dy);
            if (pushes.length < 12) {
              pushes.push({
                loser: shortUrn(aUrn),
                winner: shortUrn(bUrn),
                rule,
                dx: +push.dx.toFixed(1),
                dy: +push.dy.toFixed(1),
              });
            }
          } else {
            this.shiftGroup(aUrn, push.dx / 2, push.dy / 2);
            this.shiftGroup(bUrn, -push.dx / 2, -push.dy / 2);
            if (pushes.length < 12) {
              pushes.push({
                loser: `${shortUrn(aUrn)}+${shortUrn(bUrn)}`,
                winner: 'neither',
                rule: 'split',
                dx: +(push.dx / 2).toFixed(1),
                dy: +(push.dy / 2).toFixed(1),
              });
            }
          }
        }
      }
    }

    if (pushes.length) {
      maybeLogPushes(
        prefer ? shortUrn(prefer) : null,
        pushes,
        this.snapshotGroups().map((g) => ({
          s: g.label,
          box: [Math.round(g.box.x), Math.round(g.box.y)],
          distHome: Math.round(g.distHome),
        })),
      );
    }
  }

  private kickSiblings(groupUrn: Urn, moved: Urn): void {
    const members = this.membersOf.get(groupUrn) ?? [];

    for (const m of members) {
      if (m === moved) continue;
      const p = this.particles.get(m);
      const b = this.body(m);
      const a = this.body(moved);
      if (!p || !b || !a) continue;

      const overlapX = a.hw + b.hw + GAP - Math.abs(a.x - b.x);
      const overlapY = a.hh + b.hh + GAP - Math.abs(a.y - b.y);
      if (overlapX <= 0 || overlapY <= 0) continue;

      const dx = b.x - a.x || 0.01;
      const dy = b.y - a.y || 0.01;
      const len = Math.hypot(dx, dy) || 1;
      p.vx += (dx / len) * CARD_KICK;
      p.vy += (dy / len) * CARD_KICK;
      p.pinned = false;
    }
  }

  private body(urn: Urn): Body | null {
    const p = this.particles.get(urn);
    const kind = this.kinds.get(urn);
    if (!p || !kind) return null;
    const [hw, hh] = sizeOf(kind);
    return { urn, kind, x: p.x, y: p.y, hw, hh };
  }

  private clampParticle(urn: Urn, groupUrn: Urn): void {
    const box = this.groupBounds.get(groupUrn);
    const b = this.body(urn);
    const p = this.particles.get(urn);
    if (!box || !b || !p) return;

    const minX = box.x + INSET + b.hw;
    const maxX = box.x + box.width - INSET - b.hw;
    const minY = box.y + INSET + b.hh;
    const maxY = box.y + box.height - INSET - b.hh;

    p.x = clamp(p.x, minX, Math.max(minX, maxX));
    p.y = clamp(p.y, minY, Math.max(minY, maxY));
  }

  private resolveGroup(groupUrn: Urn, prefer?: Urn): void {
    const members = this.membersOf.get(groupUrn) ?? [];
    if (members.length < 1) return;

    for (let iter = 0; iter < PUSH_ITERS; iter += 1) {
      for (let i = 0; i < members.length; i += 1) {
        for (let j = i + 1; j < members.length; j += 1) {
          const a = this.body(members[i]);
          const b = this.body(members[j]);
          if (!a || !b) continue;
          separate(a, b, prefer);
          const pa = this.particles.get(a.urn);
          const pb = this.particles.get(b.urn);
          if (pa) {
            pa.x = a.x;
            pa.y = a.y;
          }
          if (pb) {
            pb.x = b.x;
            pb.y = b.y;
          }
        }
      }
      for (const m of members) this.clampParticle(m, groupUrn);
    }
  }

  /** Soft separation near rest — never overrides home seating. */
  private resolveGroupSoft(groupUrn: Urn): void {
    const members = this.membersOf.get(groupUrn) ?? [];
    for (let i = 0; i < members.length; i += 1) {
      for (let j = i + 1; j < members.length; j += 1) {
        const a = this.body(members[i]);
        const b = this.body(members[j]);
        if (!a || !b) continue;
        const overlapX = a.hw + b.hw + GAP - Math.abs(a.x - b.x);
        const overlapY = a.hh + b.hh + GAP - Math.abs(a.y - b.y);
        if (overlapX <= 0 || overlapY <= 0) continue;

        // Only nudge if both are already near their homes; otherwise leave springs alone.
        const ha = this.homes.get(a.urn);
        const hb = this.homes.get(b.urn);
        if (!ha || !hb) continue;
        if (Math.hypot(a.x - ha.x, a.y - ha.y) > 24) continue;
        if (Math.hypot(b.x - hb.x, b.y - hb.y) > 24) continue;

        separate(a, b);
        const pa = this.particles.get(a.urn);
        const pb = this.particles.get(b.urn);
        if (pa) {
          pa.x = a.x;
          pa.y = a.y;
        }
        if (pb) {
          pb.x = b.x;
          pb.y = b.y;
        }
      }
    }
  }

  private resolveFree(prefer?: Urn): void {
    const free = [...this.particles.keys()].filter((u) => !this.parentOf.has(u));
    for (let iter = 0; iter < PUSH_ITERS; iter += 1) {
      for (let i = 0; i < free.length; i += 1) {
        for (let j = i + 1; j < free.length; j += 1) {
          const a = this.body(free[i]);
          const b = this.body(free[j]);
          if (!a || !b) continue;
          separate(a, b, prefer);
          const pa = this.particles.get(a.urn);
          const pb = this.particles.get(b.urn);
          if (pa) {
            pa.x = a.x;
            pa.y = a.y;
          }
          if (pb) {
            pb.x = b.x;
            pb.y = b.y;
          }
        }
      }
    }
  }
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

/**
 * Spread a large separation across animation frames, while still finishing a
 * small one outright — see `GROUP_PUSH_FLOOR_PX` for why the floor is what
 * keeps easing from turning into permanent overlap.
 */
function easePush(sep: { dx: number; dy: number }, ease: number): { dx: number; dy: number } {
  const mag = Math.hypot(sep.dx, sep.dy);
  const step = Math.max(GROUP_PUSH_FLOOR_PX, mag * ease);
  if (step >= mag) return sep;
  const k = step / mag;
  return { dx: sep.dx * k, dy: sep.dy * k };
}

/**
 * Push frame `a` away from `b` by the smallest axis that clears GROUP_GAP.
 * Returns the delta to apply to `a` (or null if already clear).
 */
function separateFrames(
  a: GroupBox,
  b: GroupBox,
  gap: number,
): { dx: number; dy: number } | null {
  const acx = a.x + a.width / 2;
  const acy = a.y + a.height / 2;
  const bcx = b.x + b.width / 2;
  const bcy = b.y + b.height / 2;
  const overlapX = (a.width + b.width) / 2 + gap - Math.abs(acx - bcx);
  const overlapY = (a.height + b.height) / 2 + gap - Math.abs(acy - bcy);
  if (overlapX <= 0 || overlapY <= 0) return null;

  if (overlapX < overlapY) {
    const dir = acx <= bcx ? -1 : 1;
    return { dx: dir * overlapX, dy: 0 };
  }
  const dir = acy <= bcy ? -1 : 1;
  return { dx: 0, dy: dir * overlapY };
}

function separate(a: Body, b: Body, prefer?: Urn): void {
  const overlapX = a.hw + b.hw + GAP - Math.abs(a.x - b.x);
  const overlapY = a.hh + b.hh + GAP - Math.abs(a.y - b.y);
  if (overlapX <= 0 || overlapY <= 0) return;

  const aWeight = a.urn === prefer ? 0.1 : 0.9;
  const bWeight = b.urn === prefer ? 0.1 : 0.9;
  const sum = aWeight + bWeight;

  if (overlapX < overlapY) {
    const dir = a.x < b.x ? -1 : 1;
    a.x += (dir * overlapX * aWeight) / sum;
    b.x -= (dir * overlapX * bWeight) / sum;
  } else {
    const dir = a.y < b.y ? -1 : 1;
    a.y += (dir * overlapY * aWeight) / sum;
    b.y -= (dir * overlapY * bWeight) / sum;
  }
}
