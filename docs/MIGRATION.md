# Migration: agentless → Controller + Agents

The file-by-file consequence of [ADR-0008](adr/0008-controller-agent-topology.md).
Read the ADRs first; this document is the *what*, they are the *why*.

**Headline:** the domain kernel does not move. Identity, the graph model, the
store, the delta engine, the event bus and the entire Docker mapper are
untouched. What changes is how observations *arrive*.

---

## 1. Backend, file by file

### Untouched (≈1,250 lines)

| File | Why it survives |
|---|---|
| `core/identity.py` | The URN scheme is Controller-side by decision ([ADR-0009](adr/0009-agent-wire-protocol.md) §1). `engine_scope()` gains a second job — agent identity ([ADR-0011](adr/0011-agent-trust-and-enrollment.md)) — with no code change. |
| `core/graph/model.py` | Nodes, edges, content hashes. Nothing about them was transport-aware. |
| `core/graph/store.py` | `reconcile(kinds=…)` is exactly what an authoritative delta needs. Partition scoping becomes a security control ([ADR-0011](adr/0011-agent-trust-and-enrollment.md)). |
| `core/graph/delta.py` | Unchanged. |
| `core/ports/eventbus.py`, `infra/eventbus/memory.py` | Unchanged. |
| `core/ports/provider.py` | `Provider` and `GraphWriter` fit an agent-backed provider without modification. See §2. |
| `runtime/writer.py` | `PartitionWriter` is what makes a compromised agent unable to touch another host. Do not add a `source` argument to it, ever. |
| **`providers/docker/mapper.py`** | The 448 lines that would have been duplicated in Go. Its input is Docker's vocabulary; the agent ships exactly that. |
| `api/**` | The browser-facing contract is unaffected. |
| `frontend/**` | No change at all. |

### Deleted (≈750 lines)

| File | Replaced by |
|---|---|
| `core/ports/transport.py` | Nothing. ADR-0005's purpose — "make a remote engine reachable at a local endpoint" — is void when the engine is always already local. The Agent *is* the reach. |
| `infra/transports/ssh_tunnel.py` | The agent's outbound mTLS connection. Deletes the `asyncssh` dependency, the SSH key custody problem, and `insecure_skip_host_key_check`. |
| `infra/transports/local_socket.py` | The Go agent's local socket client. |
| `infra/transports/registry.py` | Nothing. |
| `providers/docker/client.py` | Go `net/http` over the unix socket. |
| `providers/docker/informer.py` | Go. **Move the docstring with the code** — the `since`-before-`List` ordering rationale and the coalescer explanation are the best design documentation in the repo, and the bugs they prevent are unchanged by the language. |
| `providers/docker/provider.py` | `providers/agent/provider.py`. See §2. |

Dropped dependencies: `asyncssh`, and `httpx` from the runtime path (it stays
as a test dependency for the FastAPI test client).

### New — Controller

```
core/ports/agent.py              AgentSession protocol: the port an agent connection satisfies
providers/agent/provider.py      AgentProvider — one per enrolled host
providers/agent/ingest.py        Sync/Delta frames -> mapper -> PartitionWriter
providers/agent/commands.py      Command dispatch + CommandResult correlation
api/routes/agents.py             WS endpoint for agents; enrollment; approval
infra/agentca/                   internal CA: issue, renew, revoke
proto/bystack/agent/v1/agent.proto   the contract (ADR-0009)
```

### New — Agent (Rust, [ADR-0013](adr/0013-agent-in-rust.md))

Go was the original choice; the agent was written, measured, and came in 14x
under the binary budget and 7x under the memory budget in Rust. ADR-0010 is
superseded, not wrong — only its conclusion moved.

```
agent/src/main.rs          config, the supervisor and its backoff
agent/src/docker.rs        engine client over the unix socket
agent/src/model.rs         Docker's JSON, only the fields the mapper reads
agent/src/informer.rs      List / Watch / coalesce  (the port of informer.py)
agent/src/hashset.rs       id -> content hash; change detection
agent/src/session.rs       WebSocket + protobuf framing, commands, reconnect
agent/src/wire.rs          generated types, and Docker's vocabulary poured in
agent/proto/agent.bin      checked-in descriptor set: builds without protoc
                           (enrollment lands with step 4)
```

---

## 2. The `Provider` port survives the pivot

This is the part of the original design that pays off. `Provider` is `id`,
`kind`, `start()`, `stop()`, `health()` — nothing in it assumes the provider
*initiates* anything.

What inverts is lifecycle, not interface. `DockerProvider` created a task that
dialled outward. `AgentProvider` exists as soon as the host is enrolled and
waits for a connection to bind to it.

`ProviderState` already has the right vocabulary:

| State | Agentless | Agent-backed |
|---|---|---|
| `STOPPED` | provider not started | enrolled, never connected |
| `STARTING` | opening the transport | connection accepted, awaiting `Hello` |
| `SYNCING` | initial List in flight | first `Sync` frames arriving |
| `READY` | watch established | stream live |
| `DEGRADED` | tunnel dropped, graph stale but not wrong | agent disconnected, graph stale but not wrong |
| `FAILED` | unrecoverable | certificate revoked / rejected |

`DEGRADED` keeps its exact meaning: the last known topology with a clear
marker beats blanking the screen. That was the right call when a tunnel
flapped and it is the right call when a laptop closes its lid.

`Collector` stops being a supervisor of outbound informers and becomes an
**agent registry and session supervisor**: it holds one `AgentProvider` per
enrolled host and binds inbound connections to them.

---

## 3. Configuration

The `hosts:` block described *how to reach* each host. Agents make that
question obsolete — they arrive.

```yaml
# before
hosts:
  - id: lab-01
    transport: {type: ssh_tunnel, host: 10.0.0.5, username: ops}
    resync_interval: 300

# after
agents:
  listen: "0.0.0.0:8443"        # where agents dial in; separate from the UI port
  auto_approve: false           # a valid token still requires operator approval
  cert_ttl_days: 90
  resync_interval: 900          # 15m -- the stream is local now (ADR-0008)
```

This is a **breaking config change**, and it should fail loudly rather than
migrate silently: a `hosts:` block in a config file must produce an explicit
error naming this document. `Settings.load()` already refuses to fall back to
defaults on a malformed file, for exactly this reason — a config that quietly
degraded to "manage nothing" is indistinguishable from an empty cluster.

Preserved unchanged: `read_only` defaulting to `true`, and the API binding to
loopback by default. The agent listener is a *separate* port from the UI: one
is browser-facing and may sit behind a normal reverse proxy, the other
requires client certificates.

---

## 4. Zero-config startup is preserved

`python -m bystack` currently manages the local engine with no configuration,
and that property is worth keeping — it is the whole first-run experience.

The Controller **bundles the agent binary and spawns it locally** on first
run, connecting over a unix socket instead of TLS. Same protocol, same ingest
path, same code — the only difference is the transport underneath the frames
and the absence of enrollment (a local socket with filesystem permissions is
the authentication).

Rejected: keeping the Python informer alive for the local case. It would mean
maintaining two complete implementations of discovery forever, so that the
easiest deployment could skip a subprocess. One code path is worth more than
one process.

---

## 5. Tests

| File | Fate |
|---|---|
| `test_identity.py` (95) | Unchanged. |
| `test_store.py` (231) | Unchanged. Add a case asserting a partition writer cannot affect another partition — it is a security property now. |
| `test_mapper.py` (227) | Unchanged inputs; add a check that every field the mapper reads exists in the `.proto`, so the two cannot drift ([ADR-0009](adr/0009-agent-wire-protocol.md) §3). |
| `test_eventbus.py` (77) | Unchanged. |
| `test_api.py` (171) | Unchanged. |
| `test_informer.py` (290) | **Ported.** Its scripted-engine fixtures became `bystack/conformance/engine.py`, and its cases became conformance scenarios — translated, not re-derived. |
| `test_config_and_transports.py` (118) | Transport half deleted; config half rewritten for `agents:`, plus a case asserting a legacy `hosts:` block fails loudly. |

New, and non-optional:

- **Hash field-set tests, both languages.** Pin exactly which fields feed the
  content hash, and assert `status_text` is excluded. The failure mode is not
  an error — it is a system that works perfectly and costs 100× more than it
  should, which no manual testing will surface.
- **Authoritative delta round-trip.** A `Delta` whose id set omits an entity
  must remove it from the graph; unchanged entities with no payload must
  survive.
- **Enrollment rejection paths.** Expired token, reused token, Engine ID not
  matching the certificate subject, revoked certificate, unapproved agent,
  skewed clock — each must *refuse* the connection.
- ✅ **Agent budget gates.** RSS, idle CPU and binary size, measured rather
  than asserted from a spreadsheet — see [ADR-0013](adr/0013-agent-in-rust.md).
  A budget that is only written down has already been exceeded.
- ✅ **`python -m bystack.conformance <binary>`** — a scripted Docker Engine
  plus a Controller, driving any agent binary through fourteen behaviours.
  Language-agnostic, so it survives a rewrite. It is where `test_informer.py`'s
  scenarios were translated to rather than re-derived, as §5 asks.
- ✅ **`python -m bystack.conformance.fleet <binary>`** — three agents, three
  engines, one Controller. The single-agent run verifies the *component*
  contract and is blind to everything ADR-0008 claims about a fleet: partition
  isolation, engine-scoped logical identity, cross-host image correlation,
  one agent's outage degrading one host, command routing. Twenty-seven checks.
  It found a real cross-partition write in `store.reconcile` — a host that
  stopped using a shared image deleted it for every other host still running
  it, and cut their edges. Now pinned by `test_store.py`.

The existing rule holds and gets stricter: **no test anywhere requires a
Docker daemon or a network.** The agent's informer tests run against a
scripted engine, exactly as the Python ones do today.

---

## 6. Suggested order

Each step leaves the tree working.

1. ✅ **`.proto` first.** It is the contract; both sides are written against
   it. — `proto/bystack/agent/v1/agent.proto`. Python bindings are generated
   by `backend/scripts/generate_proto.py` and **checked in**, so installing
   the Controller never needs a protobuf compiler; `test_wire.py` fails if
   the committed copy drifts from the schema.
2. ✅ **Controller ingest, agent stubbed.** `AgentProvider` + `ingest.py` +
   the WS route, driven by a synthetic frame generator in tests. The graph
   fills from fake agents. No Go yet.
3. ✅ **Agent, read-only path.** Docker client, informer, hashing, Sync/Delta —
   in Rust, and passing all fourteen conformance checks against a scripted
   engine. ✅ **The correctness gate is closed.**

       python -m bystack.conformance.parity ../agent/target/release/bystack-agent

   runs both paths against one live socket, on two Controllers with two
   stores, and compares the graphs entity by entity and edge by edge. Against
   a real daemon carrying 9 containers in 3 compose stacks: **41 entities and
   75 edges, identical by content hash.**

   It failed on its first run, which is the point of having it. Docker sends
   JSON `null` rather than `[]` or `{}` for an empty collection in a dozen
   ordinary places, and `#[serde(default)]` covers an *absent* field, not a
   null one — so the agent rejected the entire List and never synced at all.
   Every scripted fixture said `{}`, which is why fourteen and twenty-seven
   checks passed against an agent that could not read a real Docker socket.
   Fixed in `agent/src/model.rs`; the fixtures now say `null`, so the class is
   held with no daemon required.
4. **mTLS and enrollment.** Until this lands the agent listener binds to
   loopback only. *Currently: the endpoint is off by default and an unknown
   engine id is refused unless `agents.auto_approve` is set. Neither is a
   substitute for a certificate, and both say so where they are defined.*
5. ~~**Commands**, both sides, read-only enforced in both.~~ Controller side
   done — `providers/agent/commands.py` holds the `command_id` correlation
   map, and `AgentProvider` refuses dispatch to an agent that advertised
   `read_only` at `Hello`. The agent half lands with step 3.
6. **Delete the transports, the client, and the informer.** Not before step 3
   has passed — the old implementation is the oracle the new one is checked
   against. `Settings.hosts` stays until then, and only then becomes the hard
   error described in §3.
7. **Packaging**: static binaries, container image, systemd unit, and
   Controller-driven upgrade.

### What step 2 measured

The synthetic agent confirms ADR-0009's table on the real encoder, for a
compose-managed host of 100 containers:

| Frame | Size |
|---|---|
| Sync — every payload | 119.5 KB |
| Delta — 100 ids + 1 changed payload | 7.6 KB (16× cheaper) |
| Delta — steady state, ids only | 6.5 KB |
| Idle host | 0 B — no event, no frame |

And end to end against a live Controller: a full Sync produces the whole
logical layer (stack, services, `depends_on`) from labels the agent merely
forwarded; a steady-state delta moves the store's sequence number not at all;
an id dropped from the membership set removes the container *and* the service
it was the last realization of.
