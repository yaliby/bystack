# ADR-0009 — Agent wire protocol: WebSocket + Protobuf, authoritative deltas

**Status:** Accepted
**Depends on:** [ADR-0008](0008-controller-agent-topology.md)

---

## Context

[ADR-0008](0008-controller-agent-topology.md) puts an Agent on every host and
requires it to push observations to the Controller and receive commands back.
Three questions follow, and they are separable:

1. **Where is the seam?** How much does the Agent compute before sending?
2. **What is the frame?** How does a "change" get expressed without either
   re-sending everything or allowing the Controller's graph to drift?
3. **What carries it?** gRPC, WebSocket, or something else.

## Decision 1 — The Agent detects change; the Controller interprets it

The Agent runs List/Watch/coalesce against the local socket, computes a
content hash per entity, diffs against the previous hashes, and pushes only
what changed. It does not build nodes, edges or URNs, and does not know what a
stack or a service is. `mapper.py` and `identity.py` stay on the Controller,
in one language, unchanged.

Hashing is not interpretation: the Agent hashes a fixed field set and compares
integers. It never asks what a field *means*. That is the line, and it is
checkable — if a proposed agent change requires knowing Docker's semantics, it
belongs on the Controller.

### The `Status` trap, relocated

Docker's `Status` field is a **rendered string** — `"Up 3 hours"`,
`"Exited (137) 3 seconds ago"`. It changes on a wall clock, not on a state
change. Including it in a content hash means every container looks different
on every List, and incremental sync silently becomes a full refresh on a
timer.

The Controller already learned this the hard way; it is item one in the
README's "things that will bite you", and the graph store's node hash excludes
it. **That hazard now exists a second time, in the Agent**, and the Agent is
where it does the most damage — it would put the full payload set on the
network every 15 minutes, per host, forever.

So: `status_text` is **excluded from the agent-side content hash** and carried
as a field. This is correct rather than merely cheap, because the only thing
the Controller extracts from it is the exit code, and that is only meaningful
at the moment `state` changes — which *is* hashed. A container that exits gets
re-sent (state changed) and carries `Exited (137) 3 seconds ago` with it. Five
minutes later the string reads `5 minutes ago`, nothing else changed, and
nothing is sent. The exit code the Controller already recorded stays correct.

**Both agent-side hash exclusions and the Controller-side node hash must be
covered by tests that fail loudly**, in their respective languages. The failure
mode is not an error — it is a system that works perfectly and costs 100× more
than it should, which no amount of manual testing will reveal.

## Decision 2 — Authoritative deltas

The Controller's store offers `reconcile(source, nodes, edges, kinds=...)`,
which removes anything owned by the partition and absent from the call. It is
the operation that repairs whatever the event stream dropped, and it is the
only one permitted to delete entities nobody reported as gone.

A delta containing only changed entities **cannot use it**: "absent" would
mean "unchanged", and one reconcile would delete every stable container on the
host. The usual escape is two code paths — an incremental path that can drift
and a periodic full path that repairs it — and then living with the window in
between.

Instead, a `Delta` frame carries **the complete id set of the slice, plus
payloads only for entities whose hash changed.**

Ids are ~64 bytes. Payloads are ~1.8 KB. So the Controller can run a full,
authoritative `reconcile()` on *every frame*, at delta cost:

```
100 containers, one restarted
  every payload            ~180 KB
  authoritative delta        ~7 KB    (100 ids + 1 payload)
  steady state (no event)      0 B    (nothing hashed differently, nothing sent)
```

This removes the drift class entirely. There is no incremental path that can
diverge, because there is no incremental path — every frame is a reconcile.
Deletions need no separate signalling: an id that stops appearing in the set
is gone, which is also exactly how the Controller's store already thinks.

Resync survives, but its job changed. It no longer repairs the Controller's
graph — it repairs **the Agent's own hash map**, which is the only thing that
can now be wrong. That is a much smaller target, defended by re-Listing over a
unix socket and re-hashing; if the hashes agree, the resulting frame is
identical to a steady-state one and costs the same zero bytes.

Frames are scoped by slice (`CONTAINER`, `NETWORK`, `VOLUME`, `IMAGE`), which
maps onto the store's existing `kinds=` parameter. A container event refreshes
containers without claiming anything about networks — the same scoping rule
the informer already used, now expressed on the wire.

### Connection lifecycle

```
connect  ──►  Hello (agent version, engine id, capabilities, read_only)
         ◄──  HelloAck (controller epoch, resync interval, feature flags)
         ──►  Sync   ×4        full membership + all payloads, per slice
         ──►  Delta  …         authoritative, on every coalesced burst
         ◄──  Command / ResyncRequest / CertRenewal
         ──►  CommandResult
         ↕    ping / pong      every 30s, agent-initiated
```

The **first frame of every connection is a full `Sync`**. A Controller restart
therefore needs no coordination at all: the connections drop, the agents
reconnect, and every partition is rebuilt from a complete snapshot. There is
no resumption protocol, no cursor, and no server-side session state to
persist — which is only affordable because a full Sync is cheap and local.

`ping` is a WebSocket control frame with an empty payload, agent-initiated so
it also refreshes the NAT mapping. It carries **no status**. Status changes
are events, sent when they change; attaching state to a timer is precisely how
a heartbeat degrades into a polling loop.

## Decision 3 — WebSocket over TLS, carrying Protobuf

**Protobuf for the schema, WebSocket for the transport.**

### Why Protobuf

- A generated contract in both languages. No hand-written wire structs on
  either side to drift apart.
- Backward and forward compatibility by construction — non-negotiable once
  ADR-0008 makes mixed-version fleets a normal operating state.
- 3–5× smaller than the equivalent JSON, and decoded by generated code rather
  than reflection.

**The schema uses explicit typed fields, not an opaque JSON blob.** This is
the point that makes Decision 1 safe: adding a field to `mapper.py` requires
adding it to the schema, so the failure where the Agent trims away a field the
Controller quietly needed *cannot occur*. It costs a three-file change per new
field, and it is worth it — the alternative failure is silent, and manifests
as an attribute that is simply always `None` in the UI.

### Why WebSocket and not gRPC

Both are defensible; the spec permits either. WebSocket wins on the criterion
this project ranks first — overhead:

| | gRPC | WebSocket + Protobuf |
|---|---|---|
| Agent binary cost | +8–10 MB (`google.golang.org/grpc`) | +2–3 MB |
| Controller deps | `grpcio` C extension, second server, second port | already running (uvicorn serves the browser stream) |
| Reverse proxies | needs HTTP/2 end-to-end; commonly broken by default nginx config | works through anything that proxies HTTP |
| Bidirectional streaming | native | one stream, framed by us |
| Request/response correlation | generated | hand-rolled `command_id` |

The agent binary size is a stated goal, and 8 MB of gRPC machinery to carry
one stream per agent is the definition of overhead we said we would not
accept. On the Controller, running `grpc.aio` alongside uvicorn means two
servers, two ports and two lifecycles in one process, to gain multiplexing
benefits that are meaningless at one stream per agent.

What we give up is real: generated stubs and free request/response
correlation. We pay for it with a `command_id` field and a pending-command map
— perhaps 60 lines per side.

**Reversal condition.** If we ever need many concurrent independent streams
per agent, hit head-of-line blocking on a single stream, or want per-call
deadlines and cancellation as first-class primitives, switch to gRPC. **The
`.proto` does not change** — only the code that moves frames. That is why the
schema is the contract and the transport is a footnote.

## Schema sketch

Not final; it fixes the shape, and the field set is taken from what
`mapper.py` actually reads today.

```protobuf
syntax = "proto3";
package bystack.agent.v1;

message Envelope {
  uint64 seq = 1;
  oneof payload {
    Hello hello = 10;  HelloAck hello_ack = 11;
    Sync sync = 12;    Delta delta = 13;
    Command command = 20;  CommandResult command_result = 21;
    ResyncRequest resync_request = 22;
  }
}

enum Slice { SLICE_UNSPECIFIED = 0; CONTAINER = 1; NETWORK = 2; VOLUME = 3; IMAGE = 4; }

message Hello {
  string agent_version = 1;
  string engine_id     = 2;  // GET /info .ID -- also the agent's identity (ADR-0011)
  EngineInfo engine    = 3;
  bool   read_only     = 4;  // agent-side enforcement, advertised so the UI can disable actions
}

// Complete contents of a slice. Controller: reconcile(kinds=slice).
message Sync  { Slice slice = 1; repeated Entity entities = 2; }

// Complete MEMBERSHIP of a slice; payloads only where the content hash moved.
// Controller: reconcile(kinds=slice) -- authoritative, at delta cost.
message Delta {
  Slice slice = 1;
  repeated string ids   = 2;  // every entity currently in the slice
  repeated Entity changed = 3;  // subset: those whose hash changed
}

message Entity {
  string id = 1;
  oneof body { Container container = 2; Network network = 3; Volume volume = 4; Image image = 5; }
}

message EngineInfo {          // -> map_host
  string id = 1; string name = 2; string server_version = 3;
  string operating_system = 4; string kernel_version = 5; string architecture = 6;
  int32  ncpu = 7; int64 mem_total = 8;
  int32  containers_running = 9; int32 containers_total = 10;
}

message Container {           // -> map_container
  string id = 1;
  repeated string names = 2;
  string image = 3;  string image_id = 4;  string command = 5;
  int64  created = 6;
  string state = 7;           // hashed
  string status_text = 8;     // NOT hashed -- rendered string, see "the Status trap"
  map<string, string> labels = 9;
  repeated Port ports = 10;
  repeated NetworkAttachment networks = 11;
  repeated Mount mounts = 12;
}

message Port { uint32 private = 1; uint32 public = 2; string protocol = 3; string host_ip = 4; }
message NetworkAttachment { string network_id = 1; string name = 2; string ipv4 = 3; repeated string aliases = 4; }
message Mount { string type = 1; string name = 2; string destination = 3; string mode = 4; bool rw = 5; }

message Network {             // -> map_network
  string id = 1; string name = 2; string driver = 3; string scope = 4;
  bool internal = 5; bool attachable = 6; bool ingress = 7;
  repeated string subnets = 8;
  map<string, string> labels = 9;
}

message Volume {              // -> map_volume
  string name = 1; string driver = 2; string mountpoint = 3;
  string scope = 4; string created_at = 5;
  map<string, string> labels = 6;
}

message Image {               // -> map_image
  string id = 1;
  repeated string repo_tags = 2; repeated string repo_digests = 3;
  int64 size = 4; int64 created = 5;
  map<string, string> labels = 6;
}

message Command {
  string command_id = 1;      // correlates CommandResult -- the cost of not using gRPC
  string verb = 2;            // start | stop | restart | ...
  string target_id = 3;
  map<string, string> args = 4;
}
message CommandResult {
  string command_id = 1;
  bool ok = 2; string detail = 3; int32 exit_code = 4;
}
message ResyncRequest { repeated Slice slices = 1; }
```

## Consequences

- The Controller's graph can no longer drift from what an agent reported.
  Every frame is authoritative.
- Steady-state network cost per host is one 30-second ping. Not "small" —
  zero, plus liveness.
- A Controller restart is a non-event, and requires no persisted session
  state.
- Adding a mapped field is a three-file change (`.proto`, Go filler, Python
  reader). Deliberate: it converts a silent runtime failure into a
  compile-time-adjacent one.
- Two hash implementations exist (agent-side for "should I send this",
  store-side for "should I publish this"). They are independent by design and
  serve as each other's safety net; both need explicit tests pinning their
  field sets.
- A `command_id` correlation map on both sides, which gRPC would have given
  us. Small, and the reversal condition is written down.
