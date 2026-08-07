# ADR-0013 — The Agent is written in Rust

**Status:** Accepted
**Supersedes:** [ADR-0010](0010-agent-implementation-language.md)
**Depends on:** [ADR-0008](0008-controller-agent-topology.md)

---

## Context

[ADR-0010](0010-agent-implementation-language.md) chose Go, and its reasoning
was sound: a single static binary with no runtime, a footprint that matches the
budget without effort, and concurrency that is the native shape of the problem.
Every one of those arguments still holds.

What changed is that the budget stopped being a projection. The agent has now
been written and measured, and the numbers make the choice look different than
it did on paper.

The budget from ADR-0010 exists because this process runs on hardware the user
bought for something else, multiplied by the fleet size. It is the component we
can least afford to be wrong about. Under that framing, "comfortably within
budget" and "an order of magnitude under budget" are not the same answer — a
5 MB difference per host is 2.5 GB across five hundred hosts, on machines whose
owners did not ask for it.

## Decision

Write the Agent in **Rust**.

Measured, on the implementation in `agent/`, managing 100 containers against a
scripted engine:

All sizes in MiB.

| | ADR-0010 budget | Go, realistic | Rust, measured |
|---|---|---|---|
| Binary, static, stripped | < 12 | ~8–12 | **0.82** |
| RSS, after full sync | < 20 | ~10–15 | **2.92** |
| RSS, during a 200-event burst | — | — | **3.07** |
| CPU, idle | < 0.1 % | ~0 % | **0.00 %** |
| Connect + full sync of 100 containers | — | — | **25 ms** |

Fourteen times under the binary budget and seven times under the memory
budget. The Go column is an estimate — a Go runtime baseline alone is single
digit MB before any of our code — but the comparison does not turn on its
precision.

Three properties beyond the numbers:

- **No garbage collector.** The workload is a `HashMap<String, u64>` and some
  decode buffers, so GC pauses were never going to be a latency problem. What
  a GC costs here is *headroom*: a Go heap holds memory it is not using,
  against a budget where the whole point is not to hold what we do not need.
- **No runtime to start.** Cold start is what it costs to `execve` and connect.
  This matters because the agent restarts on every upgrade across the whole
  fleet.
- **`opt-level = "z"`, LTO, `panic = "abort"`, `strip`.** Each is worth its
  compile time when the artifact is copied onto every managed host.

Only the *Agent* is Rust. The Controller stays Python/FastAPI, where the
business logic, the API surface and the ecosystem are. The two meet at the
`.proto` and nowhere else, and no domain semantics cross the line
([ADR-0009](0009-agent-wire-protocol.md)).

### What this costs

Honest accounting, because the reversal condition depends on it:

- **Async Rust is harder to write than Go.** `select!` over a coalescer, a
  resync timer, a ping and an inbound stream is more ceremony than four
  goroutines and a channel. It is roughly 900 lines either way; the Rust ones
  take longer to get right.
- **Compile times are minutes, not seconds**, and cross-compiling needs the
  target's std added rather than an environment variable.
- **The contributor pool is smaller.** A Go agent is approachable to more
  people than a Rust one, and that is a real cost for a project that wants
  outside contributions.

These were weighed and lost to the measurement. The agent is small, its
surface is fixed by the `.proto`, and it is the piece that changes least — it
is the wrong component to optimise for ease of contribution and the right one
to optimise for footprint.

### `protoc` is not a build dependency

`prost-build` normally shells out to `protoc`. The agent reads a checked-in
`FileDescriptorSet` (`agent/proto/agent.bin`, produced by
`backend/scripts/generate_proto.py`) and skips it entirely, so building the
agent needs nothing but a Rust toolchain.

That matters more here than on the Controller: this is the component
cross-compiled for every architecture in a fleet, and a `protoc` dependency
would have to be satisfied in each of those toolchains.

## Alternatives considered

**Stay with Go.** The safer choice, and defensible: it meets the stated budget,
and a budget that is met is met. Rejected because the budget was written to
protect machines we do not own, and choosing 8 MB when 0.8 MB is available is
not a decision that gets easier to justify at fleet scale.

**Zig or C.** Smaller still — a few hundred KB is reachable. Rejected on
ecosystem: there is no mature WebSocket, TLS and protobuf stack in either, so
we would hand-roll framing and TLS bindings in a process holding
root-equivalent access to someone else's machine. The remaining ~700 KB is not
worth writing our own TLS glue for.

**Reversal condition.** If the agent's scope grows past observation and
command execution into something that genuinely benefits from a large
ecosystem — or if maintenance stalls because too few people can work on it —
Go remains a correct choice and the port is bounded: the `.proto` is the
contract, and `bystack.conformance` is an executable specification that any
implementation can be checked against in a single command.

## Consequences

- `agent/` is a Cargo project. `cargo build --release` produces the artifact;
  `cargo test` covers the hashing rules.
- **`python -m bystack.conformance <binary>` is the acceptance gate**, and it
  is language-agnostic. It starts a scripted Docker Engine and a Controller,
  runs the agent between them, and checks fourteen behaviours — including the
  three that are invisible at runtime (the `Status` hash exclusion, silence at
  steady state, and burst coalescing). A rewrite in any language is verified
  by the same command.
- The budget table in ARCHITECTURE §11 is revised down. It is now a measured
  ceiling rather than a projection, and `bystack.conformance` is where a
  regression would be caught.
