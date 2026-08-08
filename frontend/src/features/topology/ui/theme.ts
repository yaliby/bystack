/**
 * Visual encoding — card topology, inspired by ops dashboards that put
 * identity on the card and status on a LED, not on rainbow fills.
 *
 * Kind → card chrome (size / subtitle role). Status → LED. Stack → group frame.
 * Relationship → stroke color + dash. Never encode kind by hue alone.
 *
 * ## The intensity ladder
 *
 * A canvas where everything is bright has no foreground. So weight here is
 * spent in a fixed order, and the order is most of the design:
 *
 *   chrome        group frames, host wires, the dot grid — the page, not the
 *                 drawing. Low alpha and thin strokes, never a glow.
 *   relationships depends_on and mounts, in-band (OKLCH L≈0.6) — the structure
 *                 you opened the view to read.
 *   state         the status LEDs, small saturated marks that own the eye.
 *   selection     brightest of all, and reserved: it only ever appears under
 *                 the pointer, on a selection, or along a trace.
 *
 * Brightness spent at rest cannot signal anything. Everything at rest is
 * therefore one step quieter than it wants to be, which is what leaves room
 * for the last rung to mean something when it arrives.
 *
 * Every hue below is one this canvas already used, re-stepped for its surface
 * (holding hue, moving lightness) and checked with the dataviz validator
 * rather than by eye — including the checks that failed a first attempt. The
 * status four are the one deliberate exception to the band: they span it,
 * because their lightness *spread* is what carries the scale through colour
 * blindness.
 */

import type { EdgeKind, GraphEdge, GraphNode, NodeKind } from '../../../api/types';

export interface Palette {
  readonly surface: string;
  readonly surfaceRaised: string;
  readonly grid: string;
  readonly ink: string;
  readonly inkSecondary: string;
  readonly inkMuted: string;
  readonly nodeFill: string;
  readonly nodeStroke: string;
  readonly edge: string;
  readonly edgeHighlight: string;
  /** Card drop shadow — much softer on a light surface. */
  readonly shadow: string;
  readonly selection: string;
  readonly status: Readonly<Record<StatusRole, string>>;
  /**
   * One neutral treatment for every stack frame — see `GROUP_FRAME_*`.
   */
  readonly groupFrame: Readonly<{ stroke: string; fill: string; label: string }>;
  /** Card rail for the kinds that have no runtime state to report. */
  readonly kindAccent: Readonly<Record<'volume' | 'network' | 'default', string>>;
  readonly edgeColor: Readonly<Record<EdgeKind, string>>;
}

export type StatusRole = 'good' | 'warning' | 'serious' | 'critical' | 'neutral';

/**
 * State — the top rung, because it is the only thing here that changes by
 * itself. The four keep distinct *lightnesses*, not just distinct hues: that
 * spread is what carries the scale through colour blindness, and flattening
 * them onto one rung collapses warning↔serious to ΔE 0.9 under deuteranopia.
 * As stepped: worst adjacent ΔE 16.5 simulated / 18.5 unsimulated, all ≥ 3:1.
 */
const STATUS_DARK = {
  good: '#04ac79',
  warning: '#ffcc5e',
  serious: '#e78024',
  critical: '#b5333a',
  neutral: '#64748b',
} as const;

/**
 * Light is stepped against the light surface rather than flipped from dark.
 * `warning` lands under 3:1 here by design — a yellow that clears it has
 * stopped being yellow — so it never travels alone: the legend and the
 * inspector both spell the state out in words.
 */
const STATUS_LIGHT = {
  good: '#017651',
  warning: '#e6ad0b',
  serious: '#c06400',
  critical: '#8a001a',
  neutral: '#5a6d86',
} as const;

/**
 * Stack frames — coloured by stack name so FRONTEND / BACKEND / DATA read apart.
 * Hues are chosen to stay clear of status.good green and status.critical red.
 */
const STACK_IDENTITY = [
  '#2ec8ff', // blue — backend-ish default
  '#d070ff', // purple — frontend-ish
  '#ffb400', // amber — data
  '#00d4c8', // teal
  '#ff6b9d', // pink
  '#7c9cff', // indigo
  '#f0a020', // orange
  '#a78bfa', // violet
] as const;

/** Prefer readable roles for common compose project names. */
const STACK_ROLE_COLOR: Record<string, string> = {
  backend: '#2ec8ff',
  back: '#2ec8ff',
  api: '#2ec8ff',
  server: '#2ec8ff',
  frontend: '#d070ff',
  front: '#d070ff',
  web: '#d070ff',
  ui: '#d070ff',
  client: '#d070ff',
  data: '#ffb400',
  db: '#ffb400',
  database: '#ffb400',
  postgres: '#ffb400',
  mysql: '#ffb400',
  mongo: '#ffb400',
  redis: '#00d4c8',
  cache: '#00d4c8',
  worker: '#f0a020',
  jobs: '#f0a020',
  queue: '#f0a020',
};

function hashHue(key: string, palette: readonly string[]): string {
  let hash = 2166136261;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return palette[(hash >>> 0) % palette.length];
}

/** Stable accent for a stack group frame (FRONTEND / BACKEND / DATA…). */
export function stackIdentityColor(name: string): string {
  const key = name.trim().toLowerCase();
  if (STACK_ROLE_COLOR[key]) return STACK_ROLE_COLOR[key];
  for (const [role, color] of Object.entries(STACK_ROLE_COLOR)) {
    if (key.includes(role)) return color;
  }
  return hashHue(key || 'stack', STACK_IDENTITY);
}

const GROUP_FRAME_DARK = { stroke: '#33455f', fill: '#0f1826', label: '#8698b3' } as const;
const GROUP_FRAME_LIGHT = { stroke: '#b4c1d2', fill: '#eff3f9', label: '#5a6d86' } as const;

/**
 * Relationships — the middle rung.
 *
 * `exposed_on` is the exception that proves the ladder. It is the longest and
 * most numerous line on any real canvas and the least informative one: every
 * port it reports is already printed on the card it leaves. It keeps a colour
 * of its own so a trace can light it up, but at rest it is drawn as chrome —
 * see `EDGE_REST_ALPHA`.
 */
const EDGE_COLOR_DARK: Record<EdgeKind, string> = {
  contains: '#41597b',
  realized_by: '#41597b',
  depends_on: '#a75ddd',
  mounts: '#b07b02',
  exposed_on: '#4d647e',
  attached_to: '#495d76',
  uses_image: '#3f5068',
  hosts: '#3a4a60',
};

const EDGE_COLOR_LIGHT: Record<EdgeKind, string> = {
  contains: '#8d9cb2',
  realized_by: '#8d9cb2',
  depends_on: '#9247c5',
  mounts: '#956800',
  exposed_on: '#6b7f97',
  attached_to: '#7d8ea3',
  uses_image: '#8d9cb2',
  hosts: '#8d9cb2',
};

/**
 * What each relationship is worth before anyone touches it.
 *
 * Alpha and weight, not hue, are what build a foreground: the two links you
 * actually read the diagram for come forward, and the host wiring settles into
 * the background it belongs in. Ask about it — hover, select, trace — and it
 * comes back to full strength.
 */
export const EDGE_REST_ALPHA: Partial<Record<EdgeKind, number>> = {
  depends_on: 0.95,
  mounts: 0.8,
  exposed_on: 0.38,
};

export const EDGE_REST_WEIGHT: Partial<Record<EdgeKind, number>> = {
  depends_on: 1.05,
  mounts: 0.9,
  exposed_on: 0.6,
};

export const DARK: Palette = {
  surface: '#090d15',
  surfaceRaised: '#121a29',
  grid: '#16203a',
  ink: '#e6ecf5',
  inkSecondary: '#adbdd6',
  inkMuted: '#7286a3',
  nodeFill: '#151d2e',
  nodeStroke: '#2b3a52',
  edge: '#4d647e',
  edgeHighlight: '#cfe4f4',
  shadow: 'rgba(0,0,0,0.55)',
  selection: '#58b6e8',
  status: STATUS_DARK,
  groupFrame: GROUP_FRAME_DARK,
  kindAccent: { volume: '#56709c', network: '#56709c', default: '#41597b' },
  edgeColor: EDGE_COLOR_DARK,
};

export const LIGHT: Palette = {
  surface: '#e8ecf3',
  surfaceRaised: '#ffffff',
  grid: '#c9d3e0',
  ink: '#0f1620',
  inkSecondary: '#2c3e56',
  inkMuted: '#5a6d86',
  nodeFill: '#ffffff',
  nodeStroke: '#c1ccdb',
  edge: '#6b7f97',
  edgeHighlight: '#1a2330',
  shadow: 'rgba(15,20,25,0.14)',
  selection: '#0670e8',
  status: STATUS_LIGHT,
  groupFrame: GROUP_FRAME_LIGHT,
  kindAccent: { volume: '#8d9cb2', network: '#8d9cb2', default: '#9aa8bd' },
  edgeColor: EDGE_COLOR_LIGHT,
};

export { NODE_SIZE, shapeOf, sizeOf, type NodeShape } from '../layout/geometry';

/**
 * Per-card identity hues — stable from the service/container name so a restart
 * does not reshuffle the rainbow. Status stays on the LED; identity lives on
 * the rail and border tint.
 */
const CARD_IDENTITY = [
  '#2ec8ff',
  '#c77dff',
  '#ff8a3d',
  '#1ad4a8',
  '#ff5cad',
  '#5b8def',
  '#ffd11a',
  '#a855f7',
  '#22d3ee',
  '#fb7185',
  '#34d399',
  '#f472b6',
  '#60a5fa',
  '#fbbf24',
  '#818cf8',
] as const;

/** Stable accent for a service/container card. */
export function cardIdentityColor(node: Pick<GraphNode, 'urn' | 'name' | 'kind'>): string | null {
  if (node.kind !== 'service' && node.kind !== 'container') return null;
  return hashHue(node.name || node.urn, CARD_IDENTITY);
}

export function statusOf(node: GraphNode): StatusRole {
  // A healthcheck's verdict outranks the state for one case only. `running`
  // is still true of a container failing its probe -- the engine has not
  // stopped it and the operations it offers are a running container's -- but
  // drawing it in the same green as a working one is the map claiming
  // something it has been told is false. `starting` is not a warning: every
  // healthchecked container passes through it on the way up.
  if (node.status === 'running' && node.attrs.health === 'unhealthy') return 'warning';

  switch (node.status) {
    case 'running':
    case 'up':
      return 'good';
    case 'restarting':
    case 'paused':
    case 'removing':
      return 'warning';
    case 'created':
      return 'serious';
    case 'exited':
    case 'dead':
      return 'critical';
    default:
      return 'neutral';
  }
}

export const STATUS_LABEL: Record<StatusRole, string> = {
  good: 'Running',
  warning: 'Unstable',
  serious: 'Not started',
  critical: 'Stopped',
  neutral: 'No state',
};

export const EDGE_DASH: Record<EdgeKind, readonly number[]> = {
  contains: [],
  realized_by: [10, 4],
  // Solid — flow dots read cleanly (DockGraph active depends_on).
  depends_on: [],
  attached_to: [3, 4],
  mounts: [],
  // Dashed on purpose: this link leaves the engine, and a solid stroke would
  // read as another dependency between two things on the canvas.
  exposed_on: [7, 5],
  uses_image: [2, 5],
  hosts: [1, 8],
};

export const EDGE_LABEL: Record<EdgeKind, string> = {
  hosts: 'hosts',
  contains: 'contains',
  realized_by: 'realized by',
  attached_to: 'attached to',
  exposed_on: 'exposed on',
  mounts: 'mounts',
  uses_image: 'uses image',
  depends_on: 'depends on',
};

export const KIND_LABEL: Record<NodeKind, string> = {
  cluster: 'Cluster',
  host: 'Host',
  engine: 'Engine',
  stack: 'Stack',
  service: 'Service',
  container: 'Container',
  network: 'Network',
  volume: 'Volume',
  image: 'Image',
};

/** Prefer attrs.image, else a short kind cue for the card subtitle. */
export function cardSubtitle(node: GraphNode): string {
  const image = node.attrs.image;
  if (typeof image === 'string' && image) return image;
  if (node.kind === 'volume') {
    const driver = node.attrs.driver;
    return typeof driver === 'string' ? driver : 'volume';
  }
  if (node.kind === 'network') {
    const driver = node.attrs.driver;
    return typeof driver === 'string' ? driver : 'network';
  }
  if (node.kind === 'image') {
    const tags = node.attrs.tags;
    if (Array.isArray(tags) && typeof tags[0] === 'string') return tags[0];
  }
  return node.kind;
}

/**
 * Format published ports like `:18080 → 80`.
 *
 * Docker reports one binding per host address family, so a single published
 * port arrives twice (0.0.0.0 and ::). The card shows the mapping, not the
 * binding, so identical mappings collapse to one chip.
 */
/**
 * The chip that sits on a published-port link — `:18080 → 80`, or
 * `:8443 → 443 +1` when one container publishes several distinct mappings.
 *
 * The host end of the link is where the eye lands looking for "how do I reach
 * this", so the number belongs on the wire and not only on the card.
 */
export function edgePortLabel(edge: GraphEdge): string | null {
  const ports = edge.attrs.ports;
  if (Array.isArray(ports) && ports.length > 0) {
    const seen = new Set<string>();
    for (const entry of ports) {
      if (!entry || typeof entry !== 'object') continue;
      const row = entry as Record<string, unknown>;
      const pub = row.public;
      const priv = row.private;
      if (pub == null || priv == null) continue;
      seen.add(`:${pub} → ${priv}`);
    }
    if (seen.size > 0) {
      const [first, ...rest] = [...seen];
      return rest.length > 0 ? `${first} +${rest.length}` : first;
    }
  }

  const published = edge.attrs.published;
  if (!Array.isArray(published) || published.length === 0) return null;
  const first = `:${published[0]}`;
  return published.length > 1 ? `${first} +${published.length - 1}` : first;
}

export function cardPorts(node: GraphNode): string | null {
  const ports = node.attrs.ports;
  if (!Array.isArray(ports) || ports.length === 0) return null;
  const seen = new Set<string>();
  for (const entry of ports) {
    if (!entry || typeof entry !== 'object') continue;
    const row = entry as Record<string, unknown>;
    const pub = row.public;
    const priv = row.private;
    if (pub == null || priv == null) continue;
    seen.add(`:${pub} → ${priv}`);
    if (seen.size >= 2) break;
  }
  return seen.size ? [...seen].join('  ') : null;
}
