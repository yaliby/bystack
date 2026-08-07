# ADR-0011 — Agent trust: mTLS, single-use join tokens, Engine ID as identity

**Status:** Accepted
**Depends on:** [ADR-0008](0008-controller-agent-topology.md), ADR-0002

---

## Context

Before the pivot, trust was SSH's problem. The Controller held a private key,
the host held an authorized public key, and host key verification handled the
other direction. That is gone.

What replaces it has to answer four questions the agentless model never asked:

1. How does a Controller know that a connecting agent is who it claims to be?
2. How does an agent know it is talking to the real Controller, and not
   something that will hand it commands to run as root?
3. How does a brand-new agent get its first credential without a human copying
   a long-lived secret onto every machine?
4. What can a fully compromised agent actually do?

Question four is the one that constrains the rest. An agent holds
root-equivalent access to its host — Docker socket access *is* root — and it
runs on a machine we do not control and cannot audit.

## Decision

**Mutual TLS on every agent connection.** The Controller runs a small internal
CA whose only job is issuing and rotating agent client certificates. Agents
pin the Controller's CA; the Controller requires and verifies a client
certificate. Both directions are authenticated, always. There is no
token-in-a-header mode and no "insecure" flag — the one in the SSH transport
(`insecure_skip_host_key_check`) was already a documented footgun, and this
interface is strictly more dangerous.

**The agent's identity is the Docker Engine ID.** `GET /info` → `.ID`,
normalised by `engine_scope()`, bound into the certificate's subject. It is
already the host identity in the graph (ADR-0002) — so "which host is this"
and "which agent is this" are one question with one answer, and no second
identity concept exists to get out of sync.

This falls out correctly in both directions. An agent reinstalled, upgraded,
or moved to a new container on the same host presents the same Engine ID and
inherits that host's graph history. A host rebuilt from scratch has a new
Engine ID and is correctly a new host, rather than silently adopting a dead
machine's identity and timeline. The Controller rejects a certificate whose
subject does not match the Engine ID in the `Hello` frame, so an agent cannot
claim to be a host it is not.

**Enrollment uses a single-use, short-lived join token.** The pattern is
k3s's and Tailscale's, for the same reasons:

```
operator          bystack-ctl agent-token new --ttl 15m   ──►  one-time token
host              bystack-agent --controller wss://… --token <token>
                       │  TLS to the Controller, CA pinned
                       ├─ CSR + Engine ID + agent version
                  ◄────┤  signed client certificate (90 days)
                       └─ token is burned; never valid again
thereafter        certificate only. The token is never stored anywhere.
```

The token is a bootstrap credential and nothing else. It grants exactly one
capability — "obtain one certificate" — for a few minutes. A token leaked from
a shell history, a config-management log, or a screenshot after it was used or
expired is worth nothing. Compare a long-lived shared secret on every host,
which is worth the whole fleet forever.

**Certificates are short-lived and auto-renewed over the existing stream.**
90-day lifetime, renewal offered by the Controller at 2/3 elapsed, over the
connection that is already open and already authenticated. No cron job, no
second channel, no expiry outage. Revocation is a Controller-side allow-list
check at connection time, not a CRL — with an authoritative list of enrolled
agents already in the database, CRL machinery would be pure ceremony.

**A new enrollment requires explicit approval by default.** A valid token
yields a certificate, but the agent lands in `PENDING` and contributes nothing
to the graph until an operator approves it. Auto-approve is available and
off by default; the safe direction here is the one that makes a stolen token
visible instead of silently effective.

## What a compromised agent can do

This is the part worth being precise about, because it is the cost of the
pivot.

**It owns its host. Completely.** It always did — the Docker socket is
root-equivalent, and that is true of any process that can reach it. The
compromise of the host is not made worse by our agent being on it.

**It can lie about its own host** — invent containers, hide containers,
misreport state. The graph will believe it. There is no defence against this
short of a second independent source of truth, and we do not have one. It is
inherent to any agent-based design and should be understood, not papered over.

**It cannot touch any other host.** This is the property that matters, and it
is structural rather than a check that can be forgotten. Every provider is
handed a `GraphWriter` pre-bound to its own partition, and **there is no
`source` argument on any write method** — so a compromised agent has no way to
name another partition, and therefore no way to delete another host's
containers from the graph, forge another host's topology, or read another
host's data. It is not "we validate the source field"; it is that the source
field does not exist at that layer. That design predates the pivot (ADR-0003);
the pivot is what promotes it from hygiene to a security control.

**It cannot escalate into the Controller** beyond the graph, provided the
Controller treats agent input as untrusted — bounded frame sizes, bounded id
counts, no unbounded allocation driven by a wire field, and strict schema
validation before anything reaches the store.

**It cannot make other agents do anything.** Commands flow Controller → agent
only. There is no agent-to-agent path, and none should ever be added.

## Consequences

- The Controller becomes a CA, with the key custody obligations that implies.
  Small (one key, one purpose), but it is now a thing that can be lost — and
  losing it means re-enrolling the fleet.
- Enrollment is a real operator workflow: issue token, install agent, approve.
  Slightly more ceremony than dropping an SSH key, and each step is
  short-lived and revocable, which the SSH key was not.
- Clock skew becomes a failure mode. Certificate validation is time-sensitive,
  and a host with a badly wrong clock will fail to connect. The error must say
  so explicitly rather than surfacing as a generic TLS failure — a diagnosable
  message here is worth hours.
- Agent-side `read_only` is independent of the Controller's setting and is
  advertised in `Hello`. An agent refuses mutations on its own authority,
  because "the Controller said so" is not sufficient justification for a
  root-equivalent action, and the Controller may be misconfigured or
  compromised.
- Every one of these paths — expired token, reused token, wrong Engine ID,
  revoked certificate, unapproved agent, skewed clock — needs a test asserting
  the connection is *refused*. Authentication code that is only tested on the
  success path is not tested.
