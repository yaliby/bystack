# ByStack

An **Infrastructure Control Plane** for Docker-based infrastructure.

Not a monitoring system. It discovers your infrastructure, correlates it into
one live graph, and gives you a single place to see and operate it. Metrics,
logs and alerting stay with the systems that already do them well.

> Read [`ARCHITECTURE.md`](ARCHITECTURE.md) before changing anything, and
> [`docs/adr/`](docs/adr/) for the decisions behind it. They are load-bearing,
> and several look arbitrary until you know what goes wrong without them.
>
> Picking up unfinished work? [`docs/OPEN-WORK.md`](docs/OPEN-WORK.md) is the
> handoff: how to establish a green baseline in ninety seconds, what is left,
> in which order, and which decisions are already made and should not be
> reopened.

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
host and drove the remote socket itself. That model has been **replaced and
deleted**; see [ADR-0008](docs/adr/0008-controller-agent-topology.md) for what
it cost and [`docs/MIGRATION.md`](docs/MIGRATION.md) for what moved. **The
domain kernel did not move**: identity, graph, store, delta and the Docker
mapper are untouched, which is the claim the migration was designed to make
true and the reason the pivot cost a fortnight rather than a rewrite.

| Working now | Being built | Not built yet | Decided against |
|---|---|---|---|
| Docker discovery (List / Watch / Resync) | Controller-driven agent upgrade | Durable graph history | User authentication and RBAC ([ADR-0014](docs/adr/0014-no-user-identity.md)) |
| Canonical graph, two-layer identity | | Plugin system | Destructive operations — `remove`, `prune`, volume deletion |
| Compose stacks, services, `depends_on` | | | |
| Incremental deltas over WebSocket | | | |
| Interactive topology canvas | | | |
| Container / service / stack operations | | | |
| Agent wire protocol + Controller ingest | | | |
| mTLS, join-token enrollment, auto-renewal | | | |
| The agent — Rust, 1.97 MiB static musl, 3.9 MiB RSS | | | |
| Zero-config startup — a bundled local agent | | | |
| Hosts and the operations timeline, on the map | | | |
| Container logs — one-shot **and live** | | | |
| Crash-loop depth (`RestartCount`) | | | |
| A durable operations log | | | |
| Static binaries, an image, systemd units, a one-command install | | | |
| `bystack-ctl`, and the dashboard served by the Controller | | | |

The fourth column is not a backlog. ByStack is a **single-operator LAN control
plane**: one Controller, on a network you own, managing your own machines.
There is no second user to tell apart from the first, so a login would answer
"the one person with the password did it" — which is what having no login
already says. Because nothing authenticates, nothing destroys either: the
worst a stranger on your LAN can do is restart a container, which is
recoverable, audited, and visible on the canvas a second later. Those two
decisions hold each other up, and reversing one means reversing both.

Metrics providers are not on that list and never will be: collecting anything
Prometheus already collects is a **non-goal**, not a missing feature
([ARCHITECTURE §12](ARCHITECTURE.md#12-non-goals)). Prometheus and Grafana are
already running next to this on the same hosts. ByStack correlates with what
they know; it does not re-collect it.

**One discovery path.** The Controller reaches no Docker socket, local or
remote: agents dial in and nothing else fills the graph. The SSH transport,
the Engine HTTP client and the informer are gone
([`docs/MIGRATION.md`](docs/MIGRATION.md) §6), and they were deleted only
after the two implementations were shown to produce the same graph on the same
real daemon — 41 entities and 75 edges, identical by content hash.

**Including this machine.** `python -m bystack` with no configuration manages
the engine it is running on, by spawning the same agent binary a managed host
runs and connecting it over a unix socket instead of TLS — no CA, no token, no
approval, and no port bound anywhere
([`docs/MIGRATION.md`](docs/MIGRATION.md) §4). Same frames, same ingest, same
graph; the local case is a transport, not a second implementation.

**The tree now matches the topology diagrammed in
[ARCHITECTURE §1](ARCHITECTURE.md#1-topology)**, including the diagram's
`mTLS, one long-lived stream per agent`
([ADR-0011](docs/adr/0011-agent-trust-and-enrollment.md)). Every agent
connection is mutually authenticated against an internal CA, agents enrol with
a single-use join token, and certificates renew themselves over the stream
that is already open.

**And it now installs.** Static musl binaries per architecture, a container
image, systemd units, a wheel that carries both the agent and the dashboard,
and `bystack-ctl` ([`docs/MIGRATION.md`](docs/MIGRATION.md) §7). The dashboard
is served by the Controller on its own port, so there is no Node on a
control-plane host and no second origin. "Add a host" hands out a command that
works on a machine with nothing on it, which is what it did not do before.

One clause of that step is still open: **Controller-driven upgrade**. Version
skew is visible — the Controller knows its own version and every agent's — but
replacing a running agent is still `install-agent.sh` on that host.

---

## Installing it

Three ways in, and they differ only in what you already run. Step by step, with
the failure modes named, is [`INSTALL.md`](INSTALL.md).

```bash
# 1. The container. The Controller manages the engine it is mounted against.
git clone https://github.com/yaliby/bystack.git && cd bystack
echo "DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)" > .env
docker compose up -d --build
#    -> http://127.0.0.1:8000

# 2. A wheel. Carries the agent binary and the dashboard; nothing else needed.
python3 -m venv /opt/bystack
/opt/bystack/bin/pip install bystack-0.1.0-py3-none-manylinux*_x86_64*.whl
/opt/bystack/bin/bystack                        # manages this machine

# 3. From a checkout. `scripts/build-agent.sh` produces the static binaries.
./scripts/build-agent.sh && (cd frontend && npm ci && npm run build)
pip install -e backend && bystack
```

Adding a host is one command on that host, and the dashboard composes it with
the token already in it:

```bash
curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.1.0/scripts/install-agent.sh \
  | sudo sh -s -- --controller wss://controller:8443 --token bst1.…
```

It fetches one static binary, checks it against the release's `SHA256SUMS`,
installs a hardened systemd unit and starts it. `--binary` skips the download
for a network with no egress; `--uninstall` removes everything except the
certificate, because that is this host's identity and not ours to discard.

Everything an operator can do in the dashboard is also in `bystack-ctl`:

```bash
bystack-ctl status              # health, what is observed, whether hosts can join
bystack-ctl token               # mint a token, print the command to paste
bystack-ctl hosts               # the fleet, and which rows need a decision
bystack-ctl approve <engine-id>
```

---

## Running it

### Backend

```bash
cd agent   && cargo build --release      # the Controller spawns this for itself
cd backend
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m bystack                       # manages this machine
.venv/bin/python -m bystack --config bystack.yaml
```

Serves on `http://127.0.0.1:8000` — loopback by default, because this service
holds root-equivalent access to every engine it manages.

Zero-config means *this machine, and nothing else yet*. The Controller spawns
a local agent over a unix socket and the graph fills from it; other hosts join
by installing an agent and enabling `agents.enabled`. The Controller itself
still reaches no Docker socket — its child does, exactly as on every other
host.

If it cannot spawn one — no engine here, no binary, or `local_agent.enabled:
false` — the Hosts panel says which, because those are four different
situations that otherwise look like the same empty canvas.

- `GET  /api/v1/healthz` — service and per-provider health
- `GET  /api/v1/graph` — full snapshot
- `GET  /api/v1/graph/node?urn=…` — one node
- `GET  /api/v1/graph/node/edges?urn=…` — relationship tracing
- `GET  /api/v1/graph/node/logs?urn=…&tail=…` — a container's recent output
- `GET  /api/v1/graph/node/logs/stream?urn=…` — the same log, followed live (SSE)
- `WS   /api/v1/stream` — snapshot, then incremental deltas
- `WS   /api/v1/agents/connect` — where agents dial in (protobuf frames)
- `POST /api/v1/commands` — run an operation
- `GET  /api/v1/commands/actions?urn=…` — what is permitted on a node right now
- `GET  /api/v1/commands/audit` — recent operations, including refusals
- `GET  /docs` — OpenAPI

**Docker socket permission** is now the *agent's* problem, not the
Controller's — the Controller never opens one. The socket is owned by
`root:docker`, so on each managed host, this one included:

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

# And zero-config: the Controller spawning an agent for its own machine.
cd backend && .venv/bin/python -m bystack.conformance.local \
                 ../agent/target/release/bystack-agent
```

No test anywhere in this repo requires a Docker daemon or a network — the
agent talks to a socket, so the conformance harness gives it one.

---

## Operating it

Discovery is read-only and always on. Operations work out of the box, because
the live map is the control surface and a map that refuses every action is a
diagram ([ADR-0014](docs/adr/0014-no-user-identity.md)):

```yaml
read_only: true       # the default is false; set this to get a viewer back
```

Left alone, a container, service or stack can be started, stopped,
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
- **Nothing here deletes anything, and nothing will.** Only reversible
  lifecycle transitions exist. `remove` and `prune` are not pending work:
  [ADR-0014](docs/adr/0014-no-user-identity.md) decides this is a
  single-operator LAN control plane with no login, and a control plane with no
  authentication has no business owning a verb that destroys data. Restarting
  the wrong container is recoverable and audited; pruning the wrong volume is
  neither. The two decisions hold each other up.

**Activity** in the topbar is the timeline of everything that has been run,
including what was refused and why — a read-only Controller declining to
restart a dead service is the most useful line the log holds, and one that
showed only successes would omit exactly the entry someone came looking for.
Each row is a way back to the node: clicking it selects the target on the
canvas, and a target that has since been recreated under a new id says so
rather than offering a click that does nothing.

It survives a restart — the log is a JSON Lines file in `agents.state_dir`,
bounded at 20,000 operations and compacted in place (ADR-0012 §4a). Writes are
fsynced per record but happen off the event loop, so a slow disk delays the
operator and not every agent's connection (§4b). Every entry is attributed to
`anonymous`, permanently and by design, and the panel says so — a timeline read
as an audit log would otherwise be trusted for exactly the question it does not
answer. The same data is at `GET /api/v1/commands/audit`.

Set `audit.durable: false` for a Controller with nowhere to write; it falls
back to a bounded in-memory ring rather than refusing to start, and says so at
ERROR.

---

## The agent path

The Controller side of ADR-0008 is built. Agents dial in, push observations
and receive commands over one long-lived stream; managed hosts open no port
and publish no Docker socket.

```yaml
agents:
  enabled: true                 # binds the listener; off until asked
  listen: "0.0.0.0:8443"        # separate from the UI port, deliberately
  server_names: [controller.example]   # what the certificate covers
  auto_approve: false           # a valid token still needs operator approval
  cert_ttl_days: 90
  state_dir: "~/.local/state/bystack"  # the CA key. Back this up.
  resync_interval: 900

audit:
  durable: true                 # the operations log survives a restart
  retain: 20000                 # ADR-0006 clause 4: bounded by count, not by age
  # path: ""                    # empty means inside agents.state_dir
```

`server_names` is the field a real deployment must set: it is what agents will
have typed after `wss://`, and a certificate without that name in it fails
verification correctly and confusingly.

### Enrolling a host

From the topology map, which is where it belongs: adding a host is a change to
the picture, not a trip to a settings page.

1. **Hosts** in the topbar opens the fleet panel. **Add host** mints a join
   token and shows the command to run on the machine, ready to copy. The token
   is single-use, expires in fifteen minutes — the panel counts it down — and
   is **shown exactly once**: the Controller keeps only a digest of it, so
   closing the dialog loses it and you mint another.
2. Paste the command on the host. It assumes `bystack-agent` is already there;
   there is no installer yet (see [the agent](#the-agent)).
3. The host appears in the panel as **awaiting approval**, at the top of the
   list, and contributes nothing to the topology until you approve it there.
   That is deliberate and it is half of ADR-0011's argument: a stolen token
   produces a visible pending host rather than a silent managed one. There is
   no "approve all", and the UI never approves anything on its own.

Each row carries two badges rather than one, because *approved* and
*connected* are different questions. A host that is approved and asleep is
ordinary; so is one that is revoked and still streaming until its connection
drops. A host whose agent has gone away keeps its topology on the canvas and
is drawn as stale — the last thing it told us, which is not the same as
nothing.

Revoking asks first, by name.

The same four routes drive everything above, and are the scriptable path.
`bystack-ctl` is a thin layer over exactly these, for when curl and jq is more
typing than the job deserves:

```bash
# 1. Mint a token on the Controller. Single-use, 15 minutes by default, and it
#    carries the CA's fingerprint so the agent can authenticate the Controller
#    before sending the secret.
curl -sX POST localhost:8000/api/v1/agents/tokens -d '{"ttl_minutes": 15}' \
     -H 'content-type: application/json'
# -> {"token": "bst1.<sha256>.<secret>",
#     "install": "curl -fsSL .../install-agent.sh | sudo sh -s -- --controller ...",
#     "manual":  "bystack-agent --controller ... --token ..."}

# 2. On the managed host. `install` puts the agent there and runs it as a
#    service; `manual` is the same enrolment on a host that already has the
#    binary. The agent generates its own key, never sends it, and stores the
#    certificate it gets back.
sudo sh install-agent.sh --controller wss://controller.example:8443 --token bst1....

# 3. Approve it. Until you do, the agent is connected and contributing nothing
#    — which is what makes a stolen token visible instead of silently effective.
curl -s localhost:8000/api/v1/agents
curl -sX POST localhost:8000/api/v1/agents/<engine-id>/approve

# Revoking is the same shape, and takes effect on the next connection: it is an
# allow-list check, so there is no CRL to publish and nothing to wait for.
curl -sX POST localhost:8000/api/v1/agents/<engine-id>/revoke

# Whether a host can join at all. Minting works with the listener off — it is
# how you get to the point of turning it on — so this is the one thing a
# successful mint does not tell you.
curl -s localhost:8000/api/v1/agents/enrollment
# -> {"enabled": true, "auto_approve": false, "listen": "0.0.0.0:8443"}
```

Certificates last 90 days and are **renewed by the Controller at two thirds
elapsed, over the connection that is already open and already authenticated**.
No cron job, no second channel, no expiry outage.

### The agent

One static binary, written in Rust ([ADR-0013](docs/adr/0013-agent-in-rust.md)).

```bash
cd agent && cargo build --release
./target/release/bystack-agent --controller wss://controller:8443 --token bst1....
./target/release/bystack-agent --controller wss://controller:8443   # already enrolled
```

There is no insecure mode: no flag, no environment variable, and `ws://` is
refused with a message saying why. The SSH transport had one
(`insecure_skip_host_key_check`) and it was already a documented footgun; this
connection hands a remote party commands to run as root.

Measured managing 100 containers — the budget it is held to is in
[ADR-0010](docs/adr/0010-agent-implementation-language.md), and it comes in an
order of magnitude under it:

| | Budget | Measured (MiB) | Before mTLS |
|---|---|---|---|
| Binary, static, stripped | < 12 | **1.82** | 0.82 |
| RSS after a full sync | < 20 | **3.95** | 2.92 |
| CPU, idle | < 0.1 % | **0.00 %** | 0.00 % |
| Connect + enrol + full sync | — | **29 ms** | 25 ms |

The third column is what mutual TLS cost — rustls, `ring` and `rcgen`. Recorded
rather than quietly overwritten: "under budget" only means something if the
movement is visible. The unix-socket endpoint added 0.05 MiB on top of it,
which is the entire cost of the Controller managing its own machine.

Building it needs nothing but a Rust toolchain — no `protoc`, because the
compiled descriptor set is checked in.

### Conformance

The agent is verified by an executable specification, not by a checklist:

```bash
cd backend
.venv/bin/python -m bystack.conformance ../agent/target/release/bystack-agent
```

This starts a **scripted Docker Engine** on a unix socket and a Controller,
runs the agent between them, and drives thirty-two behaviours. It is
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
is structurally blind to all of it. Thirty checks covering partition
isolation, engine-scoped logical identity, cross-host image correlation, a
partial outage degrading one host and no other, no resurrection across a
reconnect, command routing, log reads routed the same way, and read-only
enforced at both choke points.

One host in the fixture carries a pre-25.0 colon-delimited engine id and two
share an image digest, so the two cases where an identity is spelled more than
one way are unavoidable rather than optional.

This is the run that found the shared-image partition bug below.

### Parity — the correctness gate, now retired

The two runs above script the engine, so both were blind to the same thing:
what a **real** daemon puts on the wire. `bystack.conformance.parity` pointed
the agent *and* the agentless implementation at one live socket, on two
Controllers with two stores, and compared the graphs entity by entity and edge
by edge — the agentless path being the oracle, since it had read real daemons
since before the pivot.

**It failed on its first run**, against a laptop daemon running 9 containers
in 3 compose stacks: the agent could not read a real Docker socket at all, and
no scripted run could have said so. See the `serde` entry below. With that
fixed: **41 entities and 75 edges, identical by content hash** — which closed
the gate in [`docs/MIGRATION.md`](docs/MIGRATION.md) §3 and licensed step 6.

It was deleted with the oracle it compared against; a parity harness with one
implementation left has nothing to say. The lesson it found outlived it, in
the fixtures: `conformance/engine.py` now returns `null` where a real daemon
returns `null`, so the class of defect it caught is held with no daemon
required.

> **Authenticated, both directions.** Every connection is mutual TLS against
> the Controller's internal CA, the Engine ID is bound into the certificate
> subject, and an agent whose `Hello` disagrees with its own certificate is
> refused. The listener still defaults to off — not because we cannot tell who
> is calling, but because it binds a port, usually a public one, and a
> Controller started to look at a graph should not open one unasked.

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

read_only: false              # the default; `true` makes this a viewer
api:
  host: 127.0.0.1
  port: 8000
```

A `hosts:` block is the agentless form and is now a **hard error** naming
`docs/MIGRATION.md`, rather than a silent migration. Pydantic ignores unknown
keys by default, which would have meant a Controller that started cleanly,
reported healthy and discovered nothing — indistinguishable from a cluster
that is simply empty.

---

## Layout

```
backend/src/bystack/          the Controller
  core/          domain kernel — identity, graph, ports. Imports nothing outward.
  providers/     one per external system, mutually independent
    docker/        Docker's vocabulary — the mapper, and the state->commands policy
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
- **`RestartCount` is not on `GET /containers/json`.** It exists only on the
  inspect endpoint, at every API version, so carrying it is not the addition
  of a field — it is one request per container on every List, in place of one
  per slice. The agent inspects only containers already listed as
  `restarting`: bounded, usually empty, and exactly the set the number means
  anything for. `bystack.conformance` asserts *which* ids were inspected, not
  how many, because an agent that inspects everything answers correctly and
  costs a hundred times more.
- **"Usually empty" is a claim about the median host, and the median host is
  not the one being watched.** The set above is bounded by how bad the deploy
  was, so it is also capped at 32 and issued as one batch of overlapping round
  trips rather than a serial loop. Fifty crash-looping containers used to mean
  fifty sequential inspects *ahead of every frame*, delaying the containers
  that were fine along with the ones that were not — the informer's whole cost
  model inverted, in exactly the situation an operator is staring at it.
  Conformance slows the scripted inspect to 50 ms so the two shapes are
  distinguishable at all: 0.36 s batched against 2.41 s serial.
- **Hashing a counter is only wrong when the counter is a clock.** `Status`
  advances on wall time and must not be hashed; `RestartCount` advances on an
  actual restart and must be, or a crash loop is reported once and never
  again — `restarting` is the same state on the second failure and the four
  hundredth. The cost is a resend per restart of a looping container, which is
  a host that has a problem worth a frame. `FailingStreak` sits on the other
  side of the same line and is deliberately not hashed.
- **Logs are not a command.** `CommandKind` is the closed set of *mutations* a
  read-only Controller refuses. Routing a log read through it would refuse to
  show an operator why a container is failing on the grounds that the platform
  is in its safe mode — exactly backwards. Its own frame, answered regardless
  of `read_only` on both sides.
- **A live tail needs its backpressure answered in three places, not one.**
  A container in a hot loop outruns a browser, and whichever component absorbs
  that is the one that breaks. So: the agent batches per HTTP chunk and blocks
  on a full queue, which stops it reading the daemon; the Controller keeps a
  bounded queue per subscription and drops the *oldest* lines; the browser
  holds a fixed buffer. Dropping oldest is the right loss for a live tail —
  somebody watching output scroll wants the newest lines — and every drop is
  counted and shown, because a gap the UI does not mark is a log an operator
  reads as continuous.
- **A subscription nobody cancels is a `docker logs --follow` running forever
  on someone else's machine.** So the Controller's side is a context manager,
  cancelling on any exit including the browser simply going away, and
  conformance asserts the agent actually drops its connection to the daemon —
  not merely that a cancel frame was sent.
- **Ask an agent's `Hello` what it can do; never its version.** A capability
  absent from the set is the same answer for "too old to know the frame" and
  "compiled out of this build", and the caller's decision is identical.
  Sending the frame anyway is worse than refusing: an older agent ignores it
  silently and the request times out with no diagnosis at all.
