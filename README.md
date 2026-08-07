# ByStack

An **Infrastructure Control Plane** for Docker-based infrastructure.

Not a monitoring system. It discovers your infrastructure, correlates it into
one live graph, and gives you a single place to see and operate it. Metrics,
logs and alerting stay with the systems that already do them well.

> Read [`ARCHITECTURE.md`](ARCHITECTURE.md) before changing anything, and
> [`docs/adr/`](docs/adr/) for the decisions behind it. They are load-bearing,
> and several look arbitrary until you know what goes wrong without them.

---

## Status: v0.2 — the agent path works end to end

ByStack is a **central Controller plus a lightweight Agent on each managed
host**. The Agent dials out, so managed hosts open no port and publish no
Docker socket.

```
Docker Engine ──► Agent (Rust) ─► mTLS stream ──► Controller ──► Canonical Graph ──► Topology UI
     local socket        observe · hash · push       map · correlate · decide
```

The first slice was built agentless — the Controller opened an SSH tunnel per
host and drove the remote socket itself. That model is being replaced; see
[ADR-0008](docs/adr/0008-controller-agent-topology.md) for what it cost and
[`docs/MIGRATION.md`](docs/MIGRATION.md) for what moves. **The domain kernel
does not move**: identity, graph, store, delta and the Docker mapper are
untouched.

| Working now | Being built | Not built yet |
|---|---|---|
| Docker discovery (List / Watch / Resync) | mTLS + enrollment | Authentication and RBAC |
| Canonical graph, two-layer identity | | Durable persistence & event timeline |
| Compose stacks, services, `depends_on` | | Destructive operations (needs both of the above) |
| Incremental deltas over WebSocket | | Plugin system |
| Interactive topology canvas | | |
| Container / service / stack operations | | |
| Agent wire protocol + Controller ingest | | |
| **The agent — Rust, 0.82 MiB, 2.9 MiB RSS** | | |

Metrics providers are not on that list and never will be: collecting anything
Prometheus already collects is a **non-goal**, not a missing feature
([ARCHITECTURE §12](ARCHITECTURE.md#12-non-goals)). Prometheus and Grafana are
already running next to this on the same hosts. ByStack correlates with what
they know; it does not re-collect it.

**Both discovery paths run.** The Controller accepts agent connections *and*
still drives remote sockets over SSH. That is deliberate: the agentless path
is the oracle the agent is verified against, and
[`docs/MIGRATION.md`](docs/MIGRATION.md) §6 holds it until the two produce
identical graphs on the same host. **They now do** — 41 entities and 75 edges,
identical by content hash, on a real daemon. So the SSH path is deletable, and
the two gaps between this tree and the topology diagrammed in
[ARCHITECTURE §1](ARCHITECTURE.md#1-topology) are exactly:

1. **mTLS and enrollment** ([ADR-0011](docs/adr/0011-agent-trust-and-enrollment.md))
   — the diagram's `mTLS, one long-lived stream per agent`. The endpoint is
   unauthenticated today; see the warning below.
2. **The Controller still touches remote Docker sockets** — §1 says it never
   does. True only once the transports, the client and the informer are gone.

---

## Running it

### Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m bystack                       # local engine, zero config
.venv/bin/python -m bystack --config bystack.yaml # multi-host
```

Serves on `http://127.0.0.1:8000` — loopback by default, because this service
holds root-equivalent access to every engine it manages.

- `GET  /api/v1/healthz` — service and per-provider health
- `GET  /api/v1/graph` — full snapshot
- `GET  /api/v1/graph/node?urn=…` — one node
- `GET  /api/v1/graph/node/edges?urn=…` — relationship tracing
- `WS   /api/v1/stream` — snapshot, then incremental deltas
- `WS   /api/v1/agents/connect` — where agents dial in (protobuf frames)
- `POST /api/v1/commands` — run an operation
- `GET  /api/v1/commands/actions?urn=…` — what is permitted on a node right now
- `GET  /api/v1/commands/audit` — recent operations, including refusals
- `GET  /docs` — OpenAPI

**Docker socket permission.** The socket is owned by `root:docker`. If you are
not in that group, every provider reports `degraded` with an explicit message:

```bash
sudo usermod -aG docker "$USER"   # then log out and back in
```

### Frontend

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173, proxies /api to the backend
```

### Tests

```bash
cd backend  && .venv/bin/python -m pytest && .venv/bin/ruff check src tests
cd frontend && npm run typecheck && npm test
cd agent    && cargo test

# End to end: the real agent, a scripted engine, the real Controller.
cd backend && .venv/bin/python -m bystack.conformance \
                 ../agent/target/release/bystack-agent

# And with three agents, three engines and one Controller.
cd backend && .venv/bin/python -m bystack.conformance.fleet \
                 ../agent/target/release/bystack-agent
```

No test anywhere in this repo requires a Docker daemon or a network — the
agent talks to a socket, so the conformance harness gives it one.

---

## Operating it

Discovery is read-only and always on. Operations are opt-in:

```yaml
read_only: false      # the default is true, deliberately
```

With that set, a container, service or stack can be started, stopped,
restarted, paused, resumed or killed — from the inspector panel, or directly:

```bash
curl -X POST localhost:8000/api/v1/commands \
  -H 'content-type: application/json' \
  -d '{"kind": "restart", "target": "bystack:service:<engine>/shop/web"}'
```

Three things about that are worth knowing before you rely on it:

- **A logical target fans out.** Restarting a *service* restarts every
  container realizing it; a *stack* fans out through its services. The
  response reports each container separately, and one failure among six does
  not hide the other five.
- **The graph does not change when the command returns.** It changes when
  discovery observes the transition, a moment later. The UI says
  *"Applied · waiting for discovery to confirm"* during that window rather
  than pretending it already happened — see [ADR-0012](docs/adr/0012-operations-and-audit.md).
- **Nothing here deletes anything.** Only reversible lifecycle transitions
  exist. `remove` and `prune` wait on durable audit and authentication, which
  are the things that could answer "who deleted this".

`GET /api/v1/commands/audit` returns what was attempted, including what was
refused and why. It is in-memory and does not survive a restart.

---

## The agent path

The Controller side of ADR-0008 is built. Agents dial in, push observations
and receive commands over one long-lived stream; managed hosts open no port
and publish no Docker socket.

```yaml
agents:
  enabled: true          # off by default — see the warning below
  auto_approve: false    # adopt an unknown engine id on first sight
  resync_interval: 900
```

### The agent

One static binary, written in Rust ([ADR-0013](docs/adr/0013-agent-in-rust.md)).

```bash
cd agent && cargo build --release
./target/release/bystack-agent --controller ws://controller:8000/api/v1/agents/connect
```

Measured managing 100 containers — the budget it is held to is in
[ADR-0010](docs/adr/0010-agent-implementation-language.md), and it comes in an
order of magnitude under it:

| | Budget | Measured (MiB) |
|---|---|---|
| Binary, static, stripped | < 12 | **0.82** |
| RSS after a full sync | < 20 | **2.92** |
| CPU, idle | < 0.1 % | **0.00 %** |
| Connect + full sync | — | **25 ms** |

Building it needs nothing but a Rust toolchain — no `protoc`, because the
compiled descriptor set is checked in.

### Conformance

The agent is verified by an executable specification, not by a checklist:

```bash
cd backend
.venv/bin/python -m bystack.conformance ../agent/target/release/bystack-agent
```

This starts a **scripted Docker Engine** on a unix socket and a Controller,
runs the agent between them, and drives fourteen behaviours. It is
language-agnostic: any reimplementation is checked by the same command.

Three of the checks exist because the failures are **invisible at runtime** —
the agent works perfectly, the topology looks right, and it either costs a
hundred times more than it should or silently loses a change:

- `status string excluded from the hash` — hashing Docker's rendered status
  re-sends every container on every resync, per host, forever.
- `burst coalescing` — 40 events must cost one List, not forty.
- `membership carries deletion` — a removal changes no payload, so an agent
  that only sends when a payload moved never reports it, and the container
  stays on the operator's canvas forever. This check found exactly that bug in
  our own agent.

### Fleet conformance

```bash
.venv/bin/python -m bystack.conformance.fleet ../agent/target/release/bystack-agent
```

Three real agents, three scripted engines, one Controller — because everything
ADR-0008 actually claims is a property of the *fleet*, and a single-agent run
is structurally blind to all of it. Twenty-seven checks covering partition
isolation, engine-scoped logical identity, cross-host image correlation, a
partial outage degrading one host and no other, no resurrection across a
reconnect, command routing, and read-only enforced at both choke points.

One host in the fixture carries a pre-25.0 colon-delimited engine id and two
share an image digest, so the two cases where an identity is spelled more than
one way are unavoidable rather than optional.

This is the run that found the shared-image partition bug below.

### Parity — the correctness gate

```bash
.venv/bin/python -m bystack.conformance.parity ../agent/target/release/bystack-agent
```

The two runs above script the engine, so both are blind to the same thing:
what a **real** daemon puts on the wire. This one points the agent *and* the
agentless implementation at one live socket, on two Controllers with two
stores, and compares the graphs entity by entity and edge by edge.

The agentless path is the oracle — it has read real daemons since before the
pivot — so the question is not "does the agent look right" but "do the two
implementations agree". Content is compared by the mapper's own hash, which
is what catches the defect this exists for: the agent deserializes only the
Docker fields it declares, and one it does not reaches the mapper as *absent*
rather than as an error. The result is a node that is well-formed, plausible,
and quietly missing an attribute — invisible at runtime, and invisible to a
scripted engine that was written against the same assumptions.

It mutates nothing and is safe against a daemon carrying real workloads. It
also refuses to pass on an idle socket: two empty graphs agree perfectly, and
a gate that can be closed by an empty comparison is not a gate.

**It failed on its first run**, against a laptop daemon running 9 containers
in 3 compose stacks — the agent could not read a real Docker socket at all,
and no scripted run could have said so. See the `serde` entry below. With that
fixed: **41 entities and 75 edges, identical by content hash.** That closes the
gate in [`docs/MIGRATION.md`](docs/MIGRATION.md) §3 and unblocks step 6 —
deleting the transports, the client and the informer.

> **Not yet authenticated.** ADR-0011's mTLS enrollment is the next step. Until
> it lands the endpoint trusts whoever reaches it, which is why it is off by
> default and refuses unknown engine ids unless `auto_approve` is set. Neither
> is a substitute for a certificate. Do not expose this port.

Why the delta protocol is shaped the way it is, measured on the real encoder
for a compose-managed host of 100 containers:

| Frame | Size |
|---|---|
| Every payload | 119.5 KB |
| Authoritative delta — 100 ids, 1 changed | 7.6 KB |
| Steady state — ids only | 6.5 KB |
| Idle host | 0 B |

Every frame carries the slice's **complete id set**, so the Controller runs a
full authoritative `reconcile()` on each one at delta cost — which deletes the
entire class of bug where an incremental path drifts from the reconcile path
meant to repair it. See [ADR-0009](docs/adr/0009-agent-wire-protocol.md).

Changing the contract:

```bash
$EDITOR proto/bystack/agent/v1/agent.proto
cd backend && .venv/bin/python scripts/generate_proto.py
```

The generated bindings are checked in, so installing ByStack never needs a
protobuf compiler. `test_wire.py` fails if the committed copy drifts from the
schema, and separately if `mapper.py` starts reading a Docker field the wire
does not carry.

---

## Configuration

Configuration says only *where to look*. It never describes topology — that is
discovered. See [`bystack.example.yaml`](backend/bystack.example.yaml).

Under the agent model it says even less, because agents arrive rather than
being reached:

```yaml
agents:
  listen: "0.0.0.0:8443"      # where agents dial in; separate from the UI port
  auto_approve: false         # a valid join token still needs operator approval
  resync_interval: 900        # 15m — the event stream is local now

read_only: true               # secure default; mutation is opt-in
api:
  host: 127.0.0.1
  port: 8000
```

The current `hosts:` block, with its per-host `transport:`, is the agentless
form and is documented in `bystack.example.yaml`. It becomes a hard error
rather than a silent migration — a config that quietly degraded to "manage
nothing" looks exactly like an empty cluster.

---

## Layout

```
backend/src/bystack/          the Controller
  core/          domain kernel — identity, graph, ports. Imports nothing outward.
  providers/     one per external system, mutually independent
    docker/        the agentless path — SSH/local socket, informer, mapper
    agent/         the agent path — ingest, provider, command correlation
  agent/v1/      generated wire bindings (do not edit; see scripts/generate_proto.py)
  infra/         event bus, audit log
  runtime/       collector + agent registry, the command service
  api/           FastAPI, Pydantic, WebSocket — the only layer that serializes

proto/bystack/agent/v1/       the contract — the only file both sides are
                              written against

agent/                        the Agent (Rust) — one static binary, no runtime
  src/docker.rs       local socket client
  src/informer.rs     List / Watch / coalesce
  src/hashset.rs      change detection (the Status trap lives here)
  src/session.rs      WebSocket + protobuf framing, reconnect
  proto/agent.bin     checked-in descriptor set, so no protoc to build

frontend/src/
  api/                    wire types, mirroring the backend schemas
  features/topology/
    model/                graph state machine + live subscription (no React in the state machine)
    layout/               incremental force layout and node geometry
    ui/                   canvas renderer, inspector, legend
  features/operations/
    model/                action policy, result rendering, confirmation-by-revision
    ui/                   the action bar
```

Dependencies point strictly inward. Every crossing goes through a Protocol in
`core/ports/`.

---

## Things that will bite you

Each of these cost real debugging time and is defended by a test:

- **Docker's `Status` field is a rendered string** (`"Up 3 hours"`). Putting it
  in a node's content hash re-emits every container on every reconcile and
  turns incremental sync into a full refresh on a timer. **This trap now
  exists twice** — once in the Controller's node hash, once in the Agent's
  change detection, where it would put every payload on the network every
  resync, per host, forever. Both exclusions are pinned by tests, because the
  symptom is not an error: it is a system that works perfectly and costs 100×
  more than it should.
- **A delta must carry the full id set, not just the changes.** The store's
  `reconcile` deletes whatever is absent, so a changes-only frame would delete
  every stable container on the host. Ids are cheap, payloads are not — see
  [ADR-0009](docs/adr/0009-agent-wire-protocol.md).
- **Edges are owned by one endpoint, not both.** A kind-scoped reconcile that
  claims every edge it merely touches will delete relationships it does not
  declare, forever fighting the slice that does declare them.
- **Record the watch's `since` before the initial List**, never after, or
  changes landing during the List are lost with no detectable gap.
- **`asyncio.TaskGroup` wraps errors in an `ExceptionGroup`**, so
  `except SomeError` in a supervisor silently never matches.
- **asyncssh's `known_hosts=None` disables host key verification**; `()` selects
  the default files. They are easy to swap and the failure is silent.
- **Engine IDs come in two formats** — a colon-delimited fingerprint before
  Docker 25.0, a UUID after. Both appear in a mixed fleet.
- **Docker answers a redundant start/stop with `304`, not `204`.** It is
  telling you precisely that nothing happened. Folding it into success reports
  a restart that changed nothing as a restart that worked.
- **`request.timeout or DEFAULT` is wrong for a grace period.** `timeout=0` is
  `docker stop -t 0` — "do not wait" — and truthiness turns the one operator
  who explicitly asked for no grace into the one who silently gets ten
  seconds. Read it with `is None`.
- **A command must not update the graph, however tempting.** The one-second
  window where the UI still shows the old state is the price of never
  displaying something the infrastructure did not report. See
  [ADR-0012](docs/adr/0012-operations-and-audit.md) §2.
- **Waiting on a `noop` target for confirmation hangs the UI forever.** Its
  content revision will never change, because nothing changed. Only witness
  targets that actually transitioned.
- **An authoritative delta needs a payload cache on the Controller.** The
  frame carries every id but only the changed payloads, and `reconcile` needs
  a *node* for each id. Reconciling with just the frame's payloads deletes
  every stable container on the host — the exact bug the protocol was designed
  to make impossible, reintroduced in the code that implements it.
- **Clear that cache on disconnect.** Keep it, and a reconnecting agent's
  first frame is applied against a picture from before the gap, resurrecting
  containers that were removed while it was away. Silent, and only visible as
  a container that will not go away.
- **The engine id has two spellings.** `engine_scope()` strips the colons for
  URNs, so a partition keyed by the raw id gives one host two identities:
  `node.source` says one thing and every URN says another, and
  `/graph?sources=` matches neither. Normalize at the registry, once.
- **`repeated` proto fields cannot distinguish empty from unset.** Passing
  `None` to mean "all slices" and `[]` to mean "none" encodes identically.
  Pick the reading that makes an empty list harmless.
- **A removal changes no payload.** Every surviving container hashes exactly as
  before, so an agent that decides to stay silent on "no payload moved" never
  reports a deletion — and since deletion is carried *only* by absence from the
  id set, it is then never reported at all. Membership has to be compared
  separately. Our own agent had this bug; `bystack.conformance` found it.
- **Hash Docker's labels from an ordered map.** A `HashMap` iterates
  differently per process, so hashing one makes every container look changed on
  every List. Same failure as hashing `Status`, by a different route.
- **One URN is legitimately shared by two partitions.** Image identity is the
  content digest and is deliberately *not* engine-scoped — that coincidence is
  the free cross-host correlation. It is therefore the one place a partition
  can reach outside itself, and a reconcile that deletes the node on the first
  claimant's say-so removes an image other hosts are still running and takes
  their `uses_image` edges with it. The trigger is not exotic: any host
  stopping the last container using a shared image re-projects its image slice
  and blanks that image across the entire fleet. Silent, no error anywhere, and
  it repairs itself only at the next resync — and it is a *cross-partition
  write* performed by the mechanism whose whole job is to make those
  impossible. `bystack.conformance.fleet` found it; `test_store.py` pins it.
- **`#[serde(default)]` does not cover `null`.** It covers a field that is
  *absent*; a field that is present and `null` is still a parse error, and one
  of those anywhere in a List rejects the whole List — a host that never syncs
  at all, not a field that goes missing. Docker sends `null` rather than `[]`
  or `{}` in a dozen ordinary places: `Aliases` on an endpoint, `IPAM.Config`
  on a network, `Labels` on a volume, `RepoTags` on an untagged image. Our
  fixtures all said `{}`, so fourteen and twenty-seven checks passed against an
  agent that could not read a real Docker socket at all. `bystack.conformance.
  parity` found it on its first real-daemon run; the fixtures now say `null`,
  which reproduces it with no daemon at all.
- **A content hash must be a function of content, not of container type.**
  `EMPTY` is a `MappingProxyType` and `json` cannot serialize one, so it fell
  to the encoder's fallback rather than its `sort_keys` path and was hashed as
  its *repr*. Two nodes that compare equal then get different revisions, and a
  mapping's hash depends on insertion order — which would re-upsert every node
  on every reconcile, the same failure as hashing `Status`, by a third route.
