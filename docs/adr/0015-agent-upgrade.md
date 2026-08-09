# ADR-0015 — Agent upgrade: the Controller reports skew, the host applies it

**Status:** Accepted
**Depends on:** [ADR-0008](0008-controller-agent-topology.md), [ADR-0011](0011-agent-trust-and-enrollment.md)
**Closes:** the last clause of step 7 in [`../MIGRATION.md`](../MIGRATION.md) §6

---

## Context

Step 7 named four things: static binaries, a container image, a systemd unit,
and **Controller-driven upgrade**. The first three landed as written. The
fourth turned out to name a design that this architecture cannot have in the
form the phrase suggests, and the reasons are specific enough to be worth
recording rather than rediscovering.

The problem is real. A fleet of agents is a fleet of binaries on machines
nobody logs into, and `.proto` compatibility is an operating state rather than
a migration window ([ADR-0008](0008-controller-agent-topology.md)) — which
makes "some hosts are three versions behind" normal rather than an incident,
and therefore something an operator has to be able to *see* and eventually
*act on*.

Three shapes were considered.

### 1. The agent fetches a release itself

The obvious one, and the one every `--self-update` flag in the world
implements. It does not work here, for a reason that is a deliberate property
of the agent rather than an oversight:

**The agent has no root store.** `agent/Cargo.toml` takes rustls's
underscore-prefixed `__rustls-tls` feature specifically to avoid the public CA
sets that `rustls-tls-webpki-roots` and `rustls-tls-native-roots` bring,
because the agent trusts exactly one CA — the Controller's — and a public CA
that mis-issues for the Controller's hostname must not be a way in.

Fetching from GitHub means bundling a public trust store into every agent on
every host, so that a program whose entire network surface is one pinned
connection can also talk to the internet. That is a larger change to the
threat model than the feature is worth, and it is paid on every host in the
fleet, permanently, to enable an operation that happens a few times a year.

### 2. The Controller pushes the binary down the existing stream

This one fits. The stream is already open, already mutually authenticated, and
already carries commands in that direction; a binary is a large frame rather
than a new relationship. No new port, no new trust, nothing for an operator to
open in a firewall — the properties ADR-0008 exists to preserve.

It is still not free, and the costs are what make it a decision:

- **The Controller must hold binaries for architectures it is not.** The wheel
  carries exactly one, deliberately, and the tag says which
  (`backend/hatch_build.py`). A Controller that can upgrade a mixed fleet has
  to fetch or be given the others, which reintroduces the download — moved to
  the machine that already faces the internet, which is the right machine, but
  moved rather than removed.
- **It collides with the hardening.** `packaging/systemd/bystack-agent.service`
  sets `ProtectSystem=strict`, so `/usr/local/bin` is read-only to the service.
  An agent that replaces its own executable needs that relaxed —
  `ReadWritePaths=/usr/local/bin` — which grants a network-facing daemon write
  access to a directory of executables that other things run. That is a real
  weakening of a unit whose every line was chosen, and it is the cost of
  self-replacement rather than of this transport.
- **It needs a capability gate.** Anything added to the wire that an older
  agent would not recognise must be gated on a capability it advertises at
  `Hello`, or the request times out with no diagnosis instead of refusing with
  one. That is a standing rule here, not a detail of this feature.

### 3. The host's own mechanism applies it

`install-agent.sh` already does exactly this, correctly, and is idempotent: it
fetches, verifies against `SHA256SUMS`, `install`s over the old binary — which
replaces the inode rather than writing through it, so a running agent keeps
the file it started with — and restarts the unit. Configuration management,
a package manager, or an operator with ssh does the same job.

What is missing is not the mechanism. It is knowing *which hosts need it*.

## Decision

**Split the feature at the line where the architecture actually divides it.**

### The Controller reports skew. This is built.

The Controller already learns `agent_version` from every `Hello` and knows its
own. `GET /agents` carries the version per host; the dashboard and
`bystack-ctl hosts` mark the hosts that are behind, and name the command that
fixes each one. An operator can answer "is my fleet current" without logging
into anything.

This is deliberately a *report*, not a plan. It has no state, nothing to
schedule, and nothing that can be half-applied.

### The host applies it. This is `install-agent.sh`, and stays there.

Upgrading is one command on the host, run by whatever already runs commands on
that host. It is not the Controller's job today, and the version comparison
above is what makes it a two-minute job rather than an audit.

### Pushing the binary over the stream is the design if this is ever automated.

Recorded so it is not re-derived: shape 2, gated on a capability, with the
Controller holding per-architecture binaries — and with the understanding that
the blocking cost is `ReadWritePaths` on a hardened unit, not the transport.

**A supersession of this ADR would be the right way to do it**, because
relaxing `ProtectSystem=strict` for a network-facing daemon is exactly the kind
of decision that should not arrive as an implementation detail of a convenience
feature.

## Consequences

**Version skew is visible, per host, in both surfaces.** A fleet where nothing
is behind says so; a fleet where three hosts are two versions back names them.

**Nothing in the tree pre-empts shape 2.** No wire message was added, no tag
was spent, no capability was invented. The `.proto` is unchanged, which matters
because tags are never renumbered and never reused.

**The agent gains no HTTP client and no root store.** Its network surface is
still one pinned connection to one CA, which is the property that made shape 1
unavailable and is worth more than the feature it cost.

**`install-agent.sh` is load-bearing for upgrades, not only for installs.**
Re-running it on a host is the supported upgrade path, and it is idempotent by
construction: it does not touch the certificate, it clears the unit's failure
counter so a second attempt is an attempt, and it strips a spent token rather
than leaving one that would make the next restart re-enrol.
