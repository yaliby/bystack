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
 * Four drawn kinds, four hues. Watch→host took teal; without a real colour of
 * its own, `exposed_on` collapsed into "the grey leftover" next to it. Each
 * wire now has a hue an operator can name, and rest alpha only ranks how loud
 * they are — never whether they look coloured at all.
 */
const EDGE_COLOR_DARK: Record<EdgeKind, string> = {
  contains: '#41597b',
  realized_by: '#41597b',
  depends_on: '#c084fc',
  mounts: '#e0a82e',
  // Blue — published reachability. Distinct from teal (watch) and purple (depends).
  exposed_on: '#6ba3f0',
  attached_to: '#495d76',
  uses_image: '#3f5068',
  // Teal — reserved for watch→host.
  hosts: '#2ec4a7',
  // Containment, so it reads like `contains` rather than like a dependency.
  runs_in: '#41597b',
};

const EDGE_COLOR_LIGHT: Record<EdgeKind, string> = {
  contains: '#8d9cb2',
  realized_by: '#8d9cb2',
  depends_on: '#9333ea',
  mounts: '#b45309',
  exposed_on: '#2563eb',
  attached_to: '#7d8ea3',
  uses_image: '#8d9cb2',
  hosts: '#0d9488',
  runs_in: '#8d9cb2',
};

/**
 * What each relationship is worth before anyone touches it.
 *
 * Alpha ranks attention; hue carries identity. Structure (depends / mounts)
 * leads, watch and published ports sit a step quieter — but none rest so low
 * that their colour disappears.
 */
export const EDGE_REST_ALPHA: Partial<Record<EdgeKind, number>> = {
  depends_on: 0.95,
  mounts: 0.88,
  exposed_on: 0.72,
  hosts: 0.8,
};

export const EDGE_REST_WEIGHT: Partial<Record<EdgeKind, number>> = {
  depends_on: 1.1,
  mounts: 1.0,
  exposed_on: 0.85,
  hosts: 0.95,
};

export const DARK: Palette = {
  surface: '#000000',
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

/** Kinds that have no runtime of their own — a volume is not "running". */
const STATELESS_KINDS: ReadonlySet<NodeKind> = new Set([
  'volume',
  'network',
  'image',
  'stack',
  'host',
  'engine',
  'cluster',
]);

export function statusOf(node: GraphNode): StatusRole {
  // Hosts, stacks, volumes and the like have no lifecycle to colour. The demo
  // (and any stale payload) may still carry a leftover `running`; treating it
  // as state paints a green LED on a disk and a "Running" label on a volume.
  if (STATELESS_KINDS.has(node.kind)) return 'neutral';

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
    // systemd's word for the same thing. Kept as its own case rather than
    // normalised on the way in: the graph speaks each source's vocabulary
    // (ADR-0009 §1), and the mapping from vocabulary to colour is this
    // function's whole job.
    case 'active':
      return 'good';
    case 'restarting':
    case 'paused':
    case 'removing':
    // A unit on its way up or down, and a process somebody has suspended.
    case 'activating':
    case 'deactivating':
    case 'stopped':
      return 'warning';
    case 'created':
      return 'serious';
    case 'exited':
    case 'dead':
    // `failed` is systemd's, and a zombie is a process that is present and
    // will never do anything again -- both are the state an operator came to
    // the map to find.
    case 'failed':
    case 'zombie':
      return 'critical';
    // Neither running nor broken. A stopped unit and a rule matching nothing
    // are the ordinary answer for something that is simply not there right
    // now, and drawing either in red would cry wolf on every host that has a
    // service it starts by hand.
    case 'inactive':
    case 'absent':
      return 'neutral';
    // The watch is stored and the thing is not there to watch. Serious rather
    // than critical: nothing is broken, but nothing will ever be reported
    // either, and that needs to be visible or the card reads as pending
    // forever.
    case 'not-found':
    case 'masked':
    case 'error':
      return 'serious';
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
  hosts: [5, 4],
  // Solid and short: this is containment, like `contains`, not a dependency.
  runs_in: [],
};

export const EDGE_LABEL: Record<EdgeKind, string> = {
  hosts: 'watched on',
  contains: 'contains',
  realized_by: 'realized by',
  attached_to: 'attached to',
  exposed_on: 'exposed on',
  mounts: 'mounts',
  uses_image: 'uses image',
  depends_on: 'depends on',
  runs_in: 'runs in',
};

/**
 * How an edge reads when the selected node is the *destination*.
 *
 * `EDGE_LABEL` is written from the source: `contains` means the stack contains
 * the service. Looking at the service and printing the same word makes the
 * child appear to contain its parent — which is what the inspector was doing.
 */
const EDGE_LABEL_INBOUND: Record<EdgeKind, string> = {
  hosts: 'watches',
  contains: 'in',
  realized_by: 'realizes',
  attached_to: 'has attached',
  exposed_on: 'reached via',
  mounts: 'mounted by',
  uses_image: 'used by',
  depends_on: 'depended on by',
  runs_in: 'runs',
};

/** Verb for a relationship row, from the selected node's point of view. */
export function edgeVerb(kind: EdgeKind, fromSelected: 'out' | 'in'): string {
  return fromSelected === 'out' ? EDGE_LABEL[kind] : EDGE_LABEL_INBOUND[kind];
}

/**
 * What each kind is called, everywhere a person reads it.
 *
 * **Every name is qualified by where the thing comes from**, because the
 * unqualified words collide. "Service" means a compose service here, a
 * `*.service` unit to systemd, and "the thing serving traffic" to the person
 * looking at the screen — three different objects, all of which this canvas
 * draws, on cards of the same shape. A card that says only `Service` cannot
 * be told from the other two, and the operator has to open the URN to find
 * out what they are about to restart.
 */
export const KIND_LABEL: Record<NodeKind, string> = {
  cluster: 'Cluster',
  host: 'Host',
  engine: 'Container engine',
  stack: 'Compose project',
  service: 'Compose service',
  container: 'Container',
  network: 'Docker network',
  volume: 'Docker volume',
  image: 'Image',
  unit: 'systemd unit',
  process: 'Process watch',
};

/** The same vocabulary, lowercased for the card's second line. */
const CARD_KIND: Record<NodeKind, string> = {
  cluster: 'cluster',
  host: 'host',
  engine: 'engine',
  stack: 'compose project',
  service: 'compose service',
  container: 'container',
  network: 'network',
  volume: 'volume',
  image: 'image',
  unit: 'systemd unit',
  process: 'process',
};

/**
 * One line saying what sort of object this is, in the inspector.
 *
 * The chip names the kind; this says what the kind *is*. Both are needed
 * because the names are only unambiguous to somebody who already knows the
 * model — "Compose service" and "systemd unit" are the same word to an
 * operator who has not read ADR-0016.
 */
export const KIND_HINT: Record<NodeKind, string> = {
  cluster: 'a group of machines',
  host: 'a machine running a ByStack agent',
  engine: 'the container engine on this machine',
  stack: 'a compose project — the frame around its services',
  service: 'declared in docker compose; runs as containers',
  container: 'one running container on this machine',
  network: 'a docker network',
  volume: 'docker storage — it has no runtime of its own',
  image: 'a container image',
  unit: 'a systemd unit on this host, watched because you chose it',
  process: 'a process rule — identified by what it matches, never by pid',
};

/**
 * The card's second line: the kind first, then what distinguishes this one.
 *
 * Kind first and never omitted. A compose service, a systemd unit and a
 * process watch are drawn on identically shaped cards on purpose — they are
 * all workloads and are operated the same way — so the word is the only thing
 * separating `nginx` the compose service from `nginx.service` the unit.
 *
 * `detailFrom` exists for the folded service card: the kind comes from the
 * service, the image from the container that realizes it.
 */
export function cardSubtitle(node: GraphNode, detailFrom: GraphNode = node): string {
  const kind = CARD_KIND[node.kind];
  const detail = cardDetail(detailFrom);
  return detail && detail !== kind ? `${kind} · ${detail}` : kind;
}

/** Kind-specific detail without repeating the kind word. */
export function cardDetail(node: GraphNode): string | null {
  const image = node.attrs.image;
  if (typeof image === 'string' && image) return image;
  if (node.kind === 'volume' || node.kind === 'network') {
    const driver = node.attrs.driver;
    return typeof driver === 'string' && driver ? driver : null;
  }
  if (node.kind === 'unit') {
    // The *problem* first where there is one. `sub_state` for a unit systemd
    // could not load is `dead`, which is true and reads as "it stopped" — on a
    // card for something that is not installed at all, and that an operator
    // added precisely because they are waiting for it to exist.
    const load = node.attrs.load_state;
    if (typeof load === 'string' && load && load !== 'loaded') return load;
    const sub = node.attrs.sub_state;
    return typeof sub === 'string' && sub ? sub : null;
  }
  if (node.kind === 'process') {
    // How it matches, and the readable end of what it matches. Never a pid:
    // the rule is the identity, and a pid is the one number that is different
    // after every restart. The full pattern is in the inspector — on a card it
    // would ellipsize to `/usr/local/b…`, which identifies nothing.
    return processMatch(node);
  }
  if (node.kind === 'image') {
    const tags = node.attrs.tags;
    if (Array.isArray(tags) && typeof tags[0] === 'string') return tags[0];
  }
  if (node.kind === 'host' || node.kind === 'stack') return null;
  return node.status;
}

function processMatch(node: GraphNode): string | null {
  const pattern = typeof node.attrs.pattern === 'string' ? node.attrs.pattern : '';
  const match = typeof node.attrs.match === 'string' ? node.attrs.match : '';
  if (!pattern) return match ? `${match} match` : null;
  const trimmed = pattern.replace(/\/+$/, '');
  const short = trimmed.slice(trimmed.lastIndexOf('/') + 1) || trimmed;
  return match ? `${match} ${short}` : short;
}

/**
 * Status text for the inspector header — kind-aware so a unit is not "Running".
 */
export function statusText(node: GraphNode): string {
  const role = statusOf(node);
  if (STATELESS_KINDS.has(node.kind)) return STATUS_LABEL.neutral;

  const raw = node.status;
  if (node.kind === 'unit' && raw) {
    // systemd's own words. Mapping them through the container vocabulary
    // ("Running (active)") is how the two kinds look the same in the panel.
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  }
  if (node.kind === 'process' && raw) {
    if (raw === 'running') return 'Running';
    if (raw === 'absent') return 'Not running';
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  }

  const label = STATUS_LABEL[role];
  if (!raw || role === 'neutral') return label;
  if (raw.toLowerCase() === label.toLowerCase()) return label;
  return `${label} (${raw})`;
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
