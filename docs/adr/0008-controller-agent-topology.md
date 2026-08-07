# ADR-0008 — Central Controller + lightweight Agents

**Status:** Accepted
**Supersedes:** ADR-0005 (transport abstraction), ADR-0006 §5 ("collectors are
agentless")
**Relocates:** ADR-0004 (the informer now runs in the Agent)

---

## Context

The original design was agentless. The Controller opened an SSH tunnel to each
managed host, spoke the Docker Engine HTTP API through it, and ran a
List/Watch/Resync informer per host in its own process. Nothing was installed
on the managed machines. That was a real virtue and it is what we are giving
up, so it deserves an honest accounting.

What the agentless model actually cost:

**Every managed host had to be reachable inbound.** SSH from the Controller,
or a Docker socket published on the network. Behind NAT — which is where home
labs and most small deployments live — that means port forwards, a VPN, or a
jump host. The connectivity story was the single largest obstacle to a host
being manageable at all.

**Credential blast radius.** The Controller held an SSH key granting
root-equivalent access to every host, because Docker socket access *is* root.
One compromised Controller was total fleet compromise, with credentials
sitting on disk to make it convenient.

**The network was in the hot path of observation.** The Docker event stream
crossed an SSH tunnel. Tunnels drop, and a dropped event stream is silent data
loss — which is why the resync interval was 5 minutes: not because state
drifted that fast, but because that was how quickly we wanted to notice the
stream had died. Every one of those resyncs re-Listed every host across the
network whether anything had changed or not.

**Controller cost scaled with the fleet.** N SSH tunnels, N HTTP clients, N
event streams, N asyncssh connections, all in one Python process, all needing
supervision and backoff.

The scaling target is a large number of hosts. All four costs are multiplied
by that number, and three of them are paid on the network — the most expensive
and least reliable resource in the system.

## Decision

Install a **lightweight Agent** on every managed host. The Controller becomes
the exclusive holder of decisions, state, and interfaces; the Agent observes
its local Docker socket and obeys.

**The Agent dials the Controller.** Outbound, mutually authenticated, one
long-lived stream. Managed hosts listen on nothing.

The responsibility split is stated in `ARCHITECTURE.md` §2 and is a hard line,
not a gradient: the Agent has no business logic, no database, no user model,
no dashboard, and no knowledge of the URN scheme.

## Consequences

### What gets better

**Connectivity inverts from hardest problem to non-problem.** A host behind
NAT, on a laptop, on a home connection with a dynamic IP, is exactly as
manageable as one in a datacentre. There is no port to forward, no VPN to
maintain, and no jump host. The Controller needs one reachable address; the
fleet needs zero.

**The attack surface shrinks in both directions.** No SSH keys on the
Controller. No Docker socket on any network. The Controller can no longer
reach into a host at will — it can only ask, and the Agent decides whether to
comply (§9, read-only enforced agent-side). A compromised Controller is a
serious incident rather than an automatic full-fleet root compromise.

**Observation becomes local and lossless.** The event stream is a unix socket
read on the same kernel as the daemon. There is no tunnel to drop. This is
what let resync move from 5 minutes to 15 — and that resync now re-Lists over
a unix socket, producing **zero network traffic** when nothing changed,
instead of a full remote List per host per 5 minutes.

**Steady state approaches free.** Nothing changed means nothing is hashed
differently, means no frame is sent. Idle network cost per host is a 30-second
ping.

**The Controller gets cheaper.** It sheds the tunnels, the HTTP clients, the
remote event streams, and the per-host reconnect supervision. It maintains
WebSocket connections it did not initiate.

**Per-host work parallelises for free.** Listing and hashing happen on the
host being listed, on hardware already sized for that host's workload, instead
of serialising through one Python event loop.

### What gets worse, and how we pay for it

**There is now software to install, run and upgrade on hosts we do not own.**
This is the whole cost of the decision and there is no way to make it zero. We
pay for it with:
- a single static binary, no runtime, no dependencies ([ADR-0010](0010-agent-implementation-language.md));
- an enforced resource budget (< 20 MB RSS, < 0.1% idle CPU, no disk writes);
- packaging as both a container and a systemd unit;
- Controller-driven upgrades over the existing stream, so "upgrade the fleet"
  is not an SSH-for-loop.

**A new trust boundary.** Agent input is untrusted by construction. The
existing partition-scoped `GraphWriter` already makes a compromised agent
structurally unable to affect any host but its own (ARCHITECTURE.md §6) —
this decision is what turns that from hygiene into a security control.
Enrollment and certificate lifecycle are [ADR-0011](0011-agent-trust-and-enrollment.md).

**Two languages.** Go and Python, with a schema contract between them
([ADR-0009](0009-agent-wire-protocol.md)). Mitigated by keeping *all*
interpretation on the Python side, so the Go code has no domain semantics to
drift from.

**Version skew is now a real state.** A fleet mid-upgrade runs mixed agent
versions. The wire schema must be backward-compatible by construction, which
is a large part of why it is Protobuf and not ad-hoc JSON.

**Bootstrapping is a genuine regression.** Agentless meant "point it at a host
and it works". Now a host is unmanaged until an agent is installed on it. We
buy back the zero-config case by bundling the agent with the Controller and
letting it spawn a local one over a unix socket — one code path, no special
case in the protocol. See `docs/MIGRATION.md`.

### What we are deleting

`core/ports/transport.py`, `infra/transports/{local_socket,ssh_tunnel,registry}.py`,
`providers/docker/client.py`, `providers/docker/informer.py` — roughly 750
lines of working, tested code, including the SSH tunnel that was the flagship
transport. ADR-0005's abstraction ("make a remote engine reachable at a local
endpoint") has no purpose once the engine is always already local: the Agent
*is* the reach.

`providers/docker/mapper.py` and `core/` are untouched.

## Alternatives considered

**Keep it agentless, accept the costs.** Rejected on connectivity alone. The
NAT problem has no good agentless answer, and it is the first thing a new user
hits.

**Agent as a thin Docker API proxy** — the Agent forwards the local socket
over the secure channel and the Controller's informer runs unchanged. Smallest
possible diff, and it fixes connectivity and SSH-key custody. Rejected because
it fixes nothing else: the Controller still re-Lists every host across the
network every few minutes, still holds N remote event streams, still
serialises all fleet work through one event loop. The Agent would be pure
overhead — a process on every host that makes nothing cheaper. If we are
installing software on other people's machines, it has to earn its place.

**Agent maps to graph deltas itself** — the Agent produces nodes, edges and
URNs and pushes ready-made graph deltas. Fewest bytes on the wire, by a
margin. Rejected because it puts the identity scheme and 450 lines of mapping
rules into a second language, where they must never drift; identity is already
the hardest problem in the project (ADR-0002) and cross-language duplication
is the worst possible place to solve it twice. It also violates the rule this
ADR is built on — no business logic in the Agent. The bytes it would save are
recovered almost entirely by the authoritative-delta design in
[ADR-0009](0009-agent-wire-protocol.md), at none of the cost.

**A full existing agent** (Telegraf, Beats, Netdata, node-exporter). Rejected:
every one of them is a metrics agent, we do not collect metrics
(ARCHITECTURE.md §12), and their idle footprint is one to two orders of
magnitude above our budget. We need a Docker event forwarder, which none of
them are.
