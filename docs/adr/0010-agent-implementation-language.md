# ADR-0010 — The Agent is written in Go

**Status:** **Superseded by [ADR-0013](0013-agent-in-rust.md)** — the agent was
built and measured, and Rust came in 14x under the binary budget and 7x under
the memory budget. The reasoning below is still the reasoning; only the
conclusion moved.
**Depends on:** [ADR-0008](0008-controller-agent-topology.md)

---

## Context

[ADR-0008](0008-controller-agent-topology.md) puts a process on machines we do
not own, multiplied by the fleet size. That process is the component we can
least afford to be wrong about, and its budget is the binding constraint on
the whole design:

| | Budget |
|---|---|
| RSS, steady state | < 20 MB |
| CPU, idle | < 0.1% |
| Disk | client certificate only; **zero writes at steady state** |
| Network, idle | ~200 B/min (one ping per 30s) |
| Binary | < 12 MB, static, no CGO |
| Goroutines | ~5 |
| Cold start | < 50 ms |
| Install | copy one file |

The workload is narrow and entirely I/O-bound: hold one long-lived TLS
connection, stream a unix socket, coalesce bursts, hash a few hundred small
structures, sleep. The only CPU spike is a `compose up` of twenty services,
and it is measured in milliseconds.

## Decision

Write the Agent in **Go**.

- **A single static binary with no runtime.** This is the decisive property.
  Installation is copying one file; there is no interpreter to install, no
  virtualenv, no version conflict with anything already on the host, and no
  dependency on the host's Python. `CGO_ENABLED=0` gives one binary that runs
  on any Linux of the same architecture, and cross-compiling the whole support
  matrix is a loop over `GOOS`/`GOARCH` with no toolchain per target.
- **The footprint matches the budget** without effort — a Go runtime baseline
  is single-digit MB, and this workload adds a `map[string]uint64` and some
  decode buffers to it.
- **Concurrency is the native shape of the problem.** Watch, coalesce, ping
  and command execution are four independent loops with a channel between
  them. That is a textbook Go program.
- **Fast, predictable startup**, which matters because the Agent restarts on
  every upgrade across the whole fleet.
- **The standard library covers nearly everything**: `net/http` over a unix
  socket for the Engine API, `crypto/tls` for mTLS, `encoding/json` for
  Docker's responses. Dependencies are a WebSocket library and
  `google.golang.org/protobuf`, and nothing else.

Only the *Agent* is Go. The Controller stays Python/FastAPI, where the
business logic, the API surface and the ecosystem are — the two languages meet
at the `.proto` and nowhere else, and no domain semantics cross the line
([ADR-0009](0009-agent-wire-protocol.md)).

## Alternatives considered

**Python.** One language, and the informer already exists in it — the pivot
would be mostly a move. Rejected on deployment, not performance: shipping a
Python agent means an interpreter and a dependency tree on every managed host,
which is the strongest argument *against* agents in the first place. A
CPython baseline of 25–40 MB RSS also exceeds the entire agent budget before
the program does anything, and a frozen binary (PyInstaller, Nuitka) trades
that for a 40 MB artifact and a slow, fragile build.

**Rust.** Lower footprint still and no GC. Rejected as an unfavourable trade
here: the workload is I/O-bound with no latency requirement a garbage
collector could threaten, so the gain over Go is a few MB of RSS — while the
cost is a much steeper contribution curve on the component most likely to need
quick fixes across a fleet. Async Rust in particular is a poor match for the
project's "simple to maintain" constraint.

**C.** Smallest possible footprint, and it is the wrong century for writing a
network-facing daemon with root-equivalent privileges by hand.

**Extending an existing agent** (Telegraf, Beats, Netdata). Rejected in
[ADR-0008](0008-controller-agent-topology.md): they are metrics agents, we do
not collect metrics, and their idle footprint is one to two orders of
magnitude above budget.

## Consequences

- Two languages, two toolchains, two test suites, two CI paths.
- Contributors to the Agent need Go. Contained: the Agent is small and
  deliberately dumb, so the *hard* parts of the project remain entirely in
  Python.
- Release artifacts multiply — one binary per platform, plus a container
  image and a systemd unit.
- The budget in the table above is a **CI gate, not an aspiration**: agent
  RSS, idle CPU and binary size are asserted in tests. A budget that is only
  written down is a budget that has already been exceeded.
