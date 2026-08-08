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
api/routes/agents.py             the two WS endpoints: /connect (mTLS) and /enroll
api/routes/enrollment.py         the operator's side: mint, list, approve, revoke
api/tls.py                       peer certificate -> ASGI scope; the listener's TLS context
runtime/trust.py                 enrollment, admission and renewal as one decision surface
infra/agentca/ca.py              key custody, CSR verification, issuing
infra/agentca/tokens.py          single-use, short-lived join tokens
infra/agentca/registry.py        who is enrolled, and who may connect
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
agent/src/session.rs       WebSocket + protobuf framing, commands, reconnect, renewal
agent/src/trust.rs         join token, CSR, certificate storage, the pinned-CA verifier
agent/src/enroll.rs        first contact: a token in, a certificate out
agent/src/wire.rs          generated types, and Docker's vocabulary poured in
agent/proto/agent.bin      checked-in descriptor set: builds without protoc
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
  enabled: true                 # binds the listener; off until asked
  listen: "0.0.0.0:8443"        # where agents dial in; separate from the UI port
  server_names: [controller.example, 10.0.0.2]  # what the certificate covers
  auto_approve: false           # a valid token still requires operator approval
  cert_ttl_days: 90
  max_clock_skew: 60
  state_dir: "~/.local/state/bystack"           # the CA key. Back this up.
  resync_interval: 900          # 15m -- the stream is local now (ADR-0008)
```

`server_names` is the one field a real deployment must set: it is what agents
will have typed after `wss://`, and a certificate without that name in it
fails verification correctly and confusingly. It is defaulted to loopback
rather than to a guess, because a guessed hostname produces a certificate that
is wrong in a way that reads as a bug in the CA.

`state_dir` holds the CA private key. Losing it means re-enrolling the fleet
(ADR-0011, Consequences), which makes it the one directory here worth backing
up — everything else the Controller knows is rebuilt from the agents within
seconds of a cold start.

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

## 4. Zero-config startup — ✅ **restored**

`python -m bystack` used to manage the local engine with no configuration, and
that property is worth keeping — it is the whole first-run experience.
Deleting `local_socket.py` with the rest of the agentless path (§6) removed it;
this is how it came back.

The Controller **bundles the agent binary and spawns it locally**, connecting
over a unix socket instead of TLS. Same protocol, same ingest path, same code —
the only difference is the transport underneath the frames and the absence of
enrollment (a local socket with filesystem permissions is the authentication).

```
runtime/localagent.py            find the binary, bind the socket, spawn, supervise
api/routes/agents.py             `local_router` — connect, and no enrollment
runtime/trust.py                 `admit_local` — the one line that differs
agent/src/main.rs                `Endpoint::Local`, `--controller unix:/…`
agent/src/session.rs             `dial` — one socket type for both endpoints
```

Rejected: keeping the Python informer alive for the local case. It would mean
maintaining two complete implementations of discovery forever, so that the
easiest deployment could skip a subprocess. One code path is worth more than
one process.

Four things were decisions rather than transcription:

- **The socket is bound by the Controller, not by uvicorn.** uvicorn opens a
  unix socket world-writable, and the file mode is the whole of this
  connection's authentication. It is created 0600 by umask inside a 0700
  directory, rather than chmod'd afterwards: a bind-then-chmod leaves a window
  in which any local process could connect and claim to be this host's agent.
- **The local agent is not enrolled**, and does not appear in the registry.
  Enrollment is durable operator intent (ADR-0001); nobody approved this host,
  there is nothing to revoke, and a Controller that wrote a row per machine it
  was ever run on would accumulate records no one can account for. `GET /agents`
  merges it in from the live provider set, so it appears when the agent
  connects and is gone when it does not.
- **`local_agent.enabled` defaults to on, `agents.enabled` stays off.** They
  answer different questions: this binds no port and admits no stranger; that
  one does both.
- **Why it did not start is a first-class answer.** No Docker socket, no
  binary, switched off, and "exited with status 1" are four different
  situations that otherwise render as the same empty canvas. The supervisor
  keeps the agent's own first line — the `docker` group message, most often —
  and `GET /agents/enrollment` carries it to the fleet panel.

`python -m bystack.conformance.local <binary>` drives the real supervisor, the
real listener and the real binary through nine checks, against a scripted
engine. A harness that built its own subprocess and its own socket would pass
while the Controller's wiring was broken, which is the only way this can fail.

---

## 5. Tests

| File | Fate |
|---|---|
| `test_identity.py` (95) | Unchanged. |
| `test_store.py` (231) | ✅ **Done.** Inputs unchanged, plus the partition cases this became a security property for: one provider cannot reconcile away another's nodes, and removing a shared node does not orphan the other partition — the cross-host defect `.fleet` found. |
| `test_mapper.py` (227) | ✅ **Done.** Inputs unchanged; the drift check landed in `test_wire.py` instead, next to the bindings it compares against — `test_every_docker_field_the_mapper_reads_is_carried_on_the_wire` ([ADR-0009](adr/0009-agent-wire-protocol.md) §3). |
| `test_eventbus.py` (77) | Unchanged. |
| `test_api.py` (171) | Unchanged. |
| `test_informer.py` (290) | ✅ **Ported and deleted.** Its scripted-engine fixtures became `bystack/conformance/engine.py`, and its cases became conformance scenarios — translated, not re-derived. |
| `test_config_and_transports.py` (118) | ✅ **Done**, as `test_config.py`. Transport half deleted; config half rewritten for `agents:`, plus two cases asserting a legacy `hosts:` block fails loudly and says what to do instead. |
| `test_docker_commands.py` (…) | ✅ **Split**, as `test_docker_capability.py`. The engine-status half went with the executor; the state-policy half is a pure function of a node and stayed. |

New, and non-optional:

- ✅ **Hash field-set tests, both languages.** Pin exactly which fields feed
  the content hash, and assert `status_text` is excluded. The failure mode is
  not an error — it is a system that works perfectly and costs 100× more than
  it should, which no manual testing will surface. Held on the agent's side by
  `hashset.rs` — the status string is not hashed, the state is, and labels
  hash in a stable order — on the Controller's by
  `test_agent_ingest.py::test_the_status_string_alone_never_moves_the_graph`,
  and end to end by the conformance check of the same name — a container whose
  status text alone advances must not move the store's sequence number.
- ✅ **Authoritative delta round-trip.** A `Delta` whose id set omits an entity
  must remove it from the graph; unchanged entities with no payload must
  survive. `test_agent_ingest.py`, six cases: membership carries the survivors,
  an absent id is removed, a steady state changes nothing, a payload for an id
  outside membership does not resurrect it, an id we never saw is survivable,
  and a reconnecting agent does not apply a delta against a stale cache.
- ✅ **Enrollment rejection paths.** `test_agent_trust.py`, forty-one cases:
  expired token, reused token, unknown token, a token from another
  Controller, malformed token, no client certificate at all, Engine ID not
  matching the certificate subject, a certificate from a CA that is not ours,
  a superseded certificate after re-enrolment, revoked, unapproved, skewed
  clock. Each asserts the connection is *refused*, and each asserts *which*
  refusal — an authentication layer that says no for the wrong reason is one
  refactor away from saying yes.

  Two of them found real defects while being written. An expired join token
  reported itself as "unknown", because sweeping expired digests out of the
  live set also forgot them — so an operator whose token timed out was told to
  check for a typo. And the first attempt at a broken-signature CSR flipped a
  byte of the outer DER wrapper, which the signature does not cover: the test
  passed against a CA that never checked. Both are fixed and both are pinned.
- ✅ **Agent budget gates.** RSS, idle CPU and binary size, measured rather
  than asserted from a spreadsheet — see [ADR-0013](adr/0013-agent-in-rust.md).
  A budget that is only written down has already been exceeded.
- ✅ **`python -m bystack.conformance <binary>`** — a scripted Docker Engine
  plus a Controller, driving any agent binary through seventeen behaviours
  — including the three budget gates below, so the figures in ARCHITECTURE §11
  are measured on every run rather than remembered.
  Language-agnostic, so it survives a rewrite. It is where `test_informer.py`'s
  scenarios were translated to rather than re-derived, as §5 asks.
- ✅ **`python -m bystack.conformance.local <binary>`** — the Controller
  managing its own machine, with no CA, no token, no approval and no open port
  anywhere (§4). Nine checks, driving the real supervisor and the real
  listener: that the socket is 0600 in a 0700 directory, that the graph fills
  with the same URNs any other host produces, that nothing is written to the
  enrollment registry, that a command reaches the engine and lands on the
  timeline, that a killed agent is restarted and reconnects to a full graph,
  and that shutdown leaves no socket behind.
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
4. ✅ **mTLS and enrollment.** Every agent connection is mutually
   authenticated. The Controller runs an internal CA (`infra/agentca/`:
   issue, renew, revoke), agents enrol with a single-use join token, and the
   Engine ID is bound into the certificate subject — so "which host is this"
   and "which agent is this" are one question (ADR-0011).

   The listener is its own port (`agents.listen`, default `0.0.0.0:8443`) with
   its own app, because the browser's side may sit behind an ordinary reverse
   proxy and this side must not be terminated by anything that would strip a
   client certificate.

   Three things are worth naming because they were decisions rather than
   transcription:

   - **The join token carries the CA's fingerprint** —
     `bst1.<sha256>.<secret>`. Without it, first contact would be
     trust-on-first-use and the token would go to whatever answered the
     address. This is k3s's design, for k3s's reason.
   - **Enrollment runs over the same WebSocket and the same protobuf**, on a
     path that takes no client certificate. An HTTPS POST would have been more
     conventional and would have put a TLS-capable HTTP client in the agent
     for exactly one request, against a binary budget that is measured.
   - **The enrollment registry is durable, and it is the only thing that is.**
     ADR-0001 permits it (category two: user intent plus certificate
     metadata). An operator who approved forty hosts last month did not
     consent to doing it again because the process restarted. The graph stays
     ephemeral.

   The peer certificate reaches the route through the standard ASGI TLS
   extension, which uvicorn does not populate — `api/tls.py` is the smallest
   subclass that does, and it fails closed: a connection whose certificate we
   cannot see is refused rather than admitted.

   Conformance now enrols for real. Adding a loopback exemption would have
   been easy and would have left the suite checking an agent that never
   authenticates against a Controller that never asks.

   **Cost, measured:** the agent binary went from 0.82 to **1.76 MiB** and RSS
   from 2.92 to **3.90 MiB** — rustls, ring and rcgen. Against budgets of 12
   and 20 MiB. `python -m bystack.conformance` still passes 14/14 and
   `.fleet` 27/27, now over mutual TLS.

   **Not done here:** `bystack-ctl`. The operator surface is four REST routes
   on the browser-facing port (`POST /agents/tokens`, `GET /agents`,
   `POST /agents/{id}/approve`, `POST /agents/{id}/revoke`), which is where
   the decisions belong; a CLI over them is packaging work (step 7).

   *Since: the dashboard drives those four from the topology view —
   `frontend/src/features/hosts/` — because the live map is the control
   surface and "add a host" is a change to it. One route was added for the
   UI, `GET /agents/enrollment`: minting a token succeeds whether or not the
   listener is bound, so nothing else could tell a panel that the command it
   is handing out has nothing to dial. The install command itself is still
   composed by the Controller and pasted verbatim.*
5. ~~**Commands**, both sides, read-only enforced in both.~~ Controller side
   done — `providers/agent/commands.py` holds the `command_id` correlation
   map, and `AgentProvider` refuses dispatch to an agent that advertised
   `read_only` at `Hello`. The agent half lands with step 3.
6. ✅ **Deleted the transports, the client, and the informer.** Gone:
   `infra/transports/` (SSH tunnel, local socket, registry), `core/ports/
   transport.py`, `providers/docker/{client,informer,provider}.py`, and
   `conformance/parity.py` with the oracle it compared against. `asyncssh` and
   the runtime `httpx` dependency went with them.

   What deliberately stayed: `providers/docker/mapper.py`, because ingest
   feeds Docker payloads to the *same* mapper (§5 is the reason the kernel did
   not move), and `providers/docker/capability.py` — the state→commands table,
   which never touched a socket and is the expensive thing to re-derive
   correctly later. ~~Nothing consults it at present; `AgentProvider` declines
   to, so `GET /commands/actions` is state-blind for every host.~~
   **Revisited and wired up:** `AgentProvider.supported_commands` intersects
   the table with what the agent transport can carry. The argument against —
   a cached view is a staler opinion than the check the agent applies anyway —
   was answering the wrong question: the node consulted is the node the
   dashboard is drawing, so a `Start` button under a card reading `running`
   was incoherent with itself before it was stale. Both choke points in
   ARCHITECTURE §9 still apply; this only stops an action guaranteed to fail
   from being offered. An empty answer now carries a reason too, so the UI
   explains itself instead of rendering nothing.

   `Settings.hosts` is now the hard error described in §3 — including for an
   empty `hosts: []`, because that is still a config written against the old
   model.

   ~~**Outstanding from this step:** zero-config startup (§4).~~ ✅ **Closed.**
   The Controller bundles the agent and spawns it locally over a unix socket;
   `python -m bystack` with no config manages this machine again. See §4 for
   the four decisions inside it.
7. **Packaging**: static binaries, container image, systemd unit, and
   Controller-driven upgrade.

   The one hook the Controller already has for it is
   `bystack/_bundled/bystack-agent`, first in the local agent's search order
   and empty in a source checkout — where the search falls through to
   `agent/target/release/`, so a contributor who has run `cargo build` gets the
   same first run as someone who installed a package.

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
