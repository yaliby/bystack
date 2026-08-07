# ByStack — Architecture

An Infrastructure Control Plane for Docker-based infrastructure.
Not a monitoring system. Not a Prometheus/Grafana/Portainer replacement.
It **discovers**, **correlates** and **visualizes** infrastructure, and **commands** it.

---

## 1. Topology

ByStack is a **central Controller plus a lightweight Agent on every managed
host**. The Controller never touches a remote Docker socket; the Agent never
makes a decision.

```
                        ┌──────────────┐
                        │   Dashboard  │
                        └───────┬──────┘
                     REST snapshot │ WS delta stream
                        ┌───────▼──────────────────────┐
                        │        Controller            │
                        │  API · RBAC · graph · policy │
                        │  history · scheduling        │
                        └───────┬──────────────────────┘
                    mTLS, one long-lived stream per agent
                                │  (agent dials out; nothing listens on hosts)
              ┌─────────────────┼─────────────────┐
         ┌────▼────┐       ┌────▼────┐       ┌────▼────┐
         │ Agent 1 │       │ Agent 2 │       │ Agent 3 │
         └────┬────┘       └────┬────┘       └────┬────┘
         local unix socket  (never leaves the host)
         ┌────▼────┐       ┌────▼────┐       ┌────▼────┐
         │ Docker  │       │ Docker  │       │ Docker  │
         └─────────┘       └─────────┘       └─────────┘
```

This replaces the agentless model, in which the Controller opened an SSH
tunnel to each host and drove its Docker socket remotely. See
[ADR-0008](docs/adr/0008-controller-agent-topology.md), which supersedes
ADR-0006 §5.

**The agent dials the Controller.** Not the other way round. Managed hosts open
no port, publish no Docker socket, and need no inbound firewall rule or public
address — which is what makes the model work behind NAT, where most of the
target deployments live.

---

## 2. Division of responsibility

The split is not negotiable, and it is not a gradient. Everything that
*decides* is in the Controller; everything in the Agent *observes and obeys*.

| Controller                             | Agent                              |
|----------------------------------------|------------------------------------|
| Canonical graph, identity, correlation | Local Docker socket, nothing else  |
| API, Dashboard, WebSocket to browsers  | List / Watch / coalesce            |
| AuthN, AuthZ, RBAC, users              | Change detection (content hashes)  |
| Topology construction                  | Push changed payloads              |
| Decisions, policy, scheduling          | Execute commands, report results   |
| History, audit, durable state          | Heartbeat                          |

The Agent contains **no** business logic, no database, no dashboard, no user
model, and no notion of what a "stack" or a "service" is. It does not know the
URN scheme. It ships Docker's own vocabulary upward and lets the Controller
interpret it — see §5.

---

## 3. The state model (ADR-0001)

> "Stateless" is redefined as: **no durable state that cannot be reconstructed
> from providers.**

The infrastructure graph lives **in memory** on the Controller, is ephemeral,
and is rebuildable from live sources within seconds of a cold start. It is
never the source of truth — the infrastructure is.

Durable storage (PostgreSQL) is permitted for exactly four categories, each
with a mandatory retention policy:

| Category              | Why it must persist                  | Retention        |
|-----------------------|--------------------------------------|------------------|
| Configuration         | User intent, not discoverable        | n/a (config)     |
| Credentials metadata  | References to secrets, never secrets | n/a              |
| Permissions / RBAC    | User intent                          | n/a              |
| Audit + domain events | The platform's own domain data       | bounded, TTL     |

Agent enrollment records and issued-certificate metadata join category two.
Metrics, logs and time series are **never** stored. Those systems already
exist.

The Agent stores nothing at all beyond its own client certificate. It holds no
queue, no spool, and no cache that survives a restart. An agent that has been
disconnected for a day reconnects and sends a full Sync; there is no backlog to
replay and no disk to fill.

---

## 4. Identity (ADR-0002)

The hardest problem in this project. Every entity gets a stable URN:

```
bystack:<kind>:<scope>
```

Two identity layers, because they have different lifetimes:

- **Physical** — `bystack:container:<engine_id>/<container_id>`
  Dies on every `docker compose up` that recreates the container.
- **Logical** — `bystack:service:<engine_id>/<project>/<service>`
  Survives recreation. This is what the topology graph shows and what the
  event timeline anchors to.

Host identity is the **Docker Engine ID** from `GET /info` — never an IP,
never a hostname, both of which change.
Image identity is the **content digest**, which is globally unique and
therefore correlates across hosts for free.

**The Engine ID is now also the agent's identity.** It is bound into the
agent's client certificate at enrollment, so "which host is this" and "which
agent is this" are the same question with the same answer. An agent
reinstalled on the same host keeps its graph history; an agent whose host was
rebuilt from scratch is correctly a new host. No second identity concept was
needed — see [ADR-0011](docs/adr/0011-agent-trust-and-enrollment.md).

---

## 5. Where mapping happens (ADR-0009)

> The Agent detects *that* something changed. The Controller decides *what it
> means*.

Turning Docker payloads into nodes, edges, stacks, services and `depends_on`
relationships is business logic, it is intricate, and it exists once — in
`providers/docker/mapper.py` on the Controller. Porting it into the Agent
would duplicate the URN scheme across two languages and make every identity
bug a cross-language bug.

So the Agent does the part that must be local and the Controller does the part
that must be central:

```
Agent                                    Controller
─────                                    ──────────
watch local /events        ─┐
coalesce burst (250ms)      │  observation
List affected slice         │  (must be local:
hash each entity            │   needs the socket)
diff against last hashes   ─┘
        │
        └── push only what changed ──►  map to nodes/edges/URNs  ─┐
                                        derive stacks, services   │ interpretation
                                        correlate across hosts    │ (must be central:
                                        write partition          ─┘  needs the whole graph)
```

Hashing is not interpretation. The Agent computes a content hash over a fixed
field set and compares it to the previous value; it never asks what the fields
*mean*. That keeps the seam honest.

**The wire schema is explicit, typed fields — not opaque JSON.** Adding a
field to the mapper requires adding it to the schema, so the failure mode
where the Agent trims away a field the Controller silently needed cannot
happen. See [ADR-0009](docs/adr/0009-agent-wire-protocol.md).

---

## 6. Provider independence and correlation (ADR-0003)

> No provider may depend on another provider.

But correlation across providers *is* the product. The resolution:

- Providers own a **partition** of the graph, keyed by their source id.
  They read only their own partition.
- Providers emit into the shared canonical identity namespace.
- A separate **correlation layer** (above providers, not between them) joins
  partitions using URNs and provider-declared alias hints.

An agent-backed host is a provider like any other: `AgentProvider` owns one
partition and is handed a `GraphWriter` pre-bound to it. There is no `source`
argument on any write method.

**Under the agent model this stops being hygiene and becomes a security
control.** An agent is code running on someone else's machine, and its input
is untrusted. Because its writer is structurally incapable of naming another
partition, a fully compromised agent can lie about its own host and nothing
else. It cannot delete another host's containers from the graph, cannot forge
another host's topology, and cannot see another host's data. That property was
already in the code before the pivot; the pivot is what makes it load-bearing.

**With exactly one seam.** Image identity is the content digest and is
deliberately not engine-scoped — that is the free correlation in §4 — so an
image URN is the one name two partitions legitimately both hold. The store
therefore treats a shared node as released rather than deleted until its last
claimant lets go; reconciling on the first claimant's say-so is a
cross-partition write, and `bystack.conformance.fleet` exists partly because
nothing else in the test suite could have observed it.

---

## 7. Discovery: the Informer pattern, now agent-side (ADR-0004)

Borrowed wholesale from Kubernetes client-go, because it is the proven answer
to "real-time without polling". It runs **inside the Agent** now, against a
local unix socket:

```
        ┌─── record `since` timestamp
        │
   List ┴──► full snapshot ──► full Sync frame               (authoritative)
        │
  Watch ───► /events?since=<recorded>  ──► coalesce ──► authoritative delta
        │        (no gap: since predates the List)
        │
 Resync ───► every 15 minutes, re-List and re-hash  (repairs agent-side drift)
```

Docker Engine exposes a native event stream at `GET /events`. We use it. Event
filters are applied **server-side** so noise never reaches the agent's process,
CPU, or JSON parser.

An event tells us *that* something changed and gives an id. Rather than
inspecting that id — which forces us to reason about what became orphaned,
what edges went stale, and what happens when events arrive out of order — an
event triggers a re-List of the affected slice. One local call, always
correct, no ordering assumptions. Bursts are coalesced, so a `compose up` of
twenty services costs one List rather than twenty.

**Resync got 3× cheaper.** It ran every 5 minutes because the event stream
crossed an SSH tunnel that could drop silently. The agent reads a unix socket
on the same kernel; there is no network to lose. Resync now defends only
against agent-side hash-map bugs, so 15 minutes is ample — and the re-List
never crosses the network at all. At steady state it produces zero bytes on
the wire, because nothing changed and therefore nothing is sent.

---

## 8. Communication (ADR-0009)

*Controller side built. The `.proto` is at `proto/bystack/agent/v1/`, ingest
at `providers/agent/`, the endpoint at `api/routes/agents.py`. The Go agent
and mTLS are the two remaining pieces — see `docs/MIGRATION.md` §6.*

One long-lived, bidirectional, mutually-authenticated stream per agent.
Telemetry flows up and commands flow down over the same connection.

**Transport: WebSocket over TLS, carrying Protobuf frames.** Not gRPC — the
reasoning, and the conditions under which we would switch, are in
[ADR-0009](docs/adr/0009-agent-wire-protocol.md). The message schema is the
contract; the transport underneath it is an implementation detail either side
can change without the other noticing.

Three frame families:

| Direction | Frame            | When                                       |
|-----------|------------------|--------------------------------------------|
| ↑         | `Sync`           | First frame of every connection; on resync |
| ↑         | `Delta`          | After a coalesced change burst             |
| ↑         | `CommandResult`  | After executing a command                  |
| ↓         | `Command`        | User or scheduler action                   |
| ↓         | `ResyncRequest`  | Controller detected an inconsistency       |
| ↕         | `ping` / `pong`  | Every 30s, agent-initiated                 |

### The authoritative delta

The Controller's store offers `reconcile()`, which deletes anything absent
from the call — the operation that repairs dropped events. A naive delta
containing only changed entities cannot use it, because "absent" would mean
"unchanged" and reconciling would delete the entire host.

So a `Delta` carries **the complete id set of the slice, plus payloads only
for entities whose hash changed**. Ids are cheap; payloads are not. The
Controller can therefore run a full authoritative `reconcile()` on every
single frame, at delta cost:

```
100 containers, one restarted:
  full payload set  ~180 KB
  authoritative delta  ~2 KB   (100 ids + 1 payload)
  steady state             0 B   (no event, no frame)
```

This deletes an entire class of bug. There is no "incremental path that can
drift" separate from a "reconcile path that repairs it" — every frame is a
reconcile. Resync exists only to correct the agent's own hash map, not the
Controller's graph.

Measured on the real encoder for a compose-managed host: 119.5 KB of payloads
against a 7.6 KB authoritative delta, and 6.5 KB when nothing changed at all.

The Controller pays for this with a **per-slice payload cache** — the last
payload it was given per id — because `reconcile()` needs a node for every id
and the frame only carries the changed ones. That cache is the Controller's
copy of what the agent last reported. It is ephemeral, rebuilt from the first
`Sync` of every connection, and cleared on disconnect: keeping it across a gap
would let a reconnecting agent's first delta be applied against a picture from
before the gap, silently resurrecting containers removed while it was away.

Because a reconnecting agent always sends a full `Sync` first, a Controller
restart needs no coordination: the connection drops, every agent reconnects,
and every partition is rebuilt from a complete snapshot.

### Heartbeat

An empty ping every 30 seconds, agent-initiated so it also keeps the NAT
mapping alive. It carries **no status payload**. Status changes are events and
are sent when they change; piggybacking state onto a timer is how a heartbeat
silently becomes a polling loop.

---

## 9. Commands never mutate the graph

*Built. See [ADR-0012](docs/adr/0012-operations-and-audit.md) for the
decisions, `runtime/commands.py` for the implementation.*

A `restart` request flows: **authorize → resolve → expand → audit → dispatch
→ return**, through one service that every entry point must use. The REST
route parses and delegates; it decides nothing. That indirection costs one
hop today, with a single caller, and buys the property that the second caller
— a scheduler, a webhook, the agent command path — cannot be written in a way
that forgets a check.

It does *not* optimistically update the graph. The graph changes only when the
agent's watch observes the change and pushes a delta. This gives eventual
consistency with zero drift between what we display and what is actually
running — and it survives the pivot untouched, because the agent's event
stream is the same event stream, just closer to its source.

The visible cost is a one-second window in which a successful command has not
yet reached the topology. The UI names that window rather than hiding it: it
records the target's content revision when the engine answers and reports
*"Applied · waiting for discovery to confirm"* until that revision changes.
If it never does, it says so — which is a real diagnostic, because it means
the host's event stream is gone.

**A target is a URN, and it may be a logical one.** An operator restarts the
`web` service, not `container 3f2a…`. The Controller expands a service to the
containers realizing it, and a stack through its services, by walking the same
`contains` / `realized_by` edges the mapper declared. This is the payoff for
the two-layer identity in §4, and it belongs in the Controller for exactly the
reason mapping does: only the Controller holds the whole graph.

**Only reversible lifecycle transitions exist** — start, stop, restart, pause,
unpause, kill. Nothing destroys state. That is a sequencing decision, not a
difficulty: `remove` is one more HTTP call, but it is only defensible once the
platform can answer *who deleted the database volume*, and the audit log is
still an in-memory ring with every entry attributed to `anonymous`.
Destructive operations, durable audit and authentication arrive together.

Read-only mode is enforced at **two** choke points now, deliberately:

1. The Controller's command layer, before dispatch — one place, as before.
2. The Agent itself, before touching the socket.

The second is not redundancy for its own sake. The Agent holds
root-equivalent access to a machine, and "the Controller said so" is not an
acceptable sole justification for acting on it. An agent configured read-only
refuses mutations regardless of what arrives on the wire, and advertises that
at enrollment so the UI can disable the actions rather than offer them and
fail.

---

## 10. Real-time delivery to browsers

Unchanged by the pivot — the Controller is still the only thing a browser
talks to.

```
WS connect ──► {type: "snapshot", seq: N, graph: {...}}
           ──► {type: "delta", seq: N+1, ...}
           ──► {type: "delta", seq: N+2, ...}
```

- Monotonic `seq`; a client that detects a gap re-requests a snapshot.
- **Scoped subscriptions** — a client viewing one host does not receive deltas
  for the other 499.
- **Bounded per-client queues.** On overflow we drop the client's backlog and
  mark it `lagging`, forcing a resnapshot. We never grow a buffer to keep up
  with a slow consumer.

The same bounded-queue rule applies to agent connections, in the other
direction: an agent that floods is throttled and asked to resync, never
buffered without limit.

---

## 11. Resource budgets (ADR-0006, revised)

> The control plane must consume significantly less than the infrastructure it
> manages. It must never become the bottleneck.

The Agent budget is the strict one, because it is multiplied by the fleet and
runs on hardware the user bought for something else.

| Scope                     | CPU idle | RAM     | Disk       | Net idle    |
|---------------------------|----------|---------|------------|-------------|
| Agent                     | < 0.1%   | < 20 MB | cert only  | ~200 B/min  |
| Controller (small fleet)  | < 5%     | < 512MB | Postgres   | —           |
| Controller (medium fleet) | < 5%     | < 2 GB  | Postgres   | —           |

**The agent budget is now measured rather than projected.** Written in Rust
([ADR-0013](docs/adr/0013-agent-in-rust.md)), managing 100 containers:

Sizes are **MiB**. The same figures in decimal MB — what most size reporters
print — are 0.86 and 3.06; the budgets are quoted from ADR-0010 as written,
and the headroom is large enough that the distinction never decides anything.

| | Budget | Measured (MiB) |
|---|---|---|
| Binary, static, stripped | < 12 | **0.82** |
| RSS after a full sync | < 20 | **2.92** |
| RSS during a 200-event burst | — | **3.07** |
| CPU, idle | < 0.1 % | **0.00 %** |
| Connect + full sync, 100 containers | — | **25 ms** |

Its working set is the Docker payloads it is currently hashing plus a
`HashMap<id, u64>` — roughly 12 KB for 100 containers. When nothing is
happening it is blocked in `epoll` and costs nothing measurable, and it makes
**zero disk writes at steady state**.

`python -m bystack.conformance <agent-binary>` is where these are re-checked,
alongside the behaviours that are invisible at runtime.

The Controller got *cheaper* in the pivot: it no longer maintains N SSH
tunnels, N HTTP clients and N remote event streams, and it no longer re-Lists
every host over the network every five minutes.

Design consequences, non-negotiable:

1. **Pydantic lives at the edges only.** The hot graph store uses frozen
   `slots` dataclasses. 5,000 Pydantic instances cost tens of MB for nothing.
2. **Content hashing for change detection**, now on both sides: the Agent
   hashes to decide what to send, the store hashes to decide what to publish.
   O(1) instead of O(attrs), and the two are independent safety nets.
3. **No polling where an event stream exists.** Preference order:
   native event stream → push → incremental sync → periodic reconcile.
4. **Every table has a retention policy.** No unbounded growth, anywhere.
5. ~~Collectors are agentless.~~ **Superseded by
   [ADR-0008](docs/adr/0008-controller-agent-topology.md).** Agents are
   installed on managed hosts. The cost this clause was protecting against —
   an unbounded footprint on machines we do not own — is now defended by the
   Agent budget above, which is enforced rather than assumed.

### The question every feature must answer

Before any capability is added to the Agent:

> **Can this be done with less overhead on the Agent?**

If several designs work: take the one that costs less, keep it simple, and do
not move responsibility to the Agent unless it genuinely cannot live in the
Controller. The Agent is on hardware we do not own, multiplied by the fleet
size, and it is the component we can least afford to be wrong about.

The four original questions still apply to every feature anywhere: *Does it
duplicate an existing system? What is its CPU/RAM cost? Can it be event-driven
instead of polled? Can it be optional?*

---

## 12. Non-goals

Storing metrics. Storing logs. Replacing Prometheus, Grafana, Loki,
Alertmanager, Docker, Portainer, or Kubernetes dashboards. Collecting anything
Prometheus already collects.

**Having an agent on every host does not change this.** It makes it harder to
hold, which is exactly why it is written down: once a process is sitting next
to the Docker socket, "while we're here, let's stream
`/containers/{id}/stats`" is a five-line change that would quietly turn a
control plane into a second-rate cAdvisor — and one that costs real CPU per
container, on every managed host, forever. The Agent reports **its own**
health and coarse state counts. Per-container resource time series belong to
the system that already does them well.

Where an official API exists, we consume it. Where an official SDK exists and
fits the architecture, we prefer it — with one deliberate exception documented
in ADR-0007: the official `docker-py` SDK is synchronous and would block the
event loop. That exception is now moot on the Controller, which no longer
speaks to Docker at all; the Agent speaks the Engine HTTP API directly over
the Unix socket, on a minimal `hyper` HTTP/1 client, for the same reason that
always applied — a small, stable, well-documented subset beats a heavyweight
dependency. (`hyper` rather than a hand-rolled client for the reason recorded
in `agent/Cargo.toml`: the chunked-transfer decoder sits on the path that
carries every event, and its correctness is the whole real-time story.)

---

## Architecture decision records

ADR-0001 through ADR-0007 are recorded inline above. Decisions from the
Controller/Agent pivot onward are separate documents in
[`docs/adr/`](docs/adr/):

| ADR | Decision |
|-----|----------|
| [0008](docs/adr/0008-controller-agent-topology.md) | Controller + lightweight agents; supersedes ADR-0006 §5 |
| [0009](docs/adr/0009-agent-wire-protocol.md) | WebSocket + Protobuf, authoritative deltas, where mapping lives |
| [0010](docs/adr/0010-agent-implementation-language.md) | The Agent is written in Go |
| [0011](docs/adr/0011-agent-trust-and-enrollment.md) | mTLS, join tokens, Engine ID as agent identity |
| [0013](docs/adr/0013-agent-in-rust.md) | The Agent is written in Rust; supersedes ADR-0010 |
| [0012](docs/adr/0012-operations-and-audit.md) | Operations: lifecycle only, logical targets, no optimistic updates |
