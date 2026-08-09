# ADR-0014 — No user identity; the network is the boundary

**Status:** Accepted
**Depends on:** [ADR-0011](0011-agent-trust-and-enrollment.md), [ADR-0012](0012-operations-and-audit.md)
**Closes:** the item `docs/OPEN-WORK.md` §4 held open

---

## Context

Three things were named as arriving together before any destructive operation
could exist: a durable audit log, an answer to *who*, and RBAC. Two of the
three were built. The third — authentication — was left open with three
candidate shapes: local accounts, OIDC, or a trusted reverse-proxy header.

It was left open because the question underneath it had not been answered:
**who is this for, and what is it deployed onto?**

The answer is a single-operator LAN control plane. One Controller, on a
network the operator owns, managing agents on their own machines. There is no
second user to distinguish from the first, no tenant boundary, no delegation,
and nobody to hold accountable who is not the person who installed it. In that
deployment a login screen does not answer "who deleted the database volume" —
it answers "the one person with the password did", which is what the absence
of a login screen already says.

Every candidate identity model was priced against that reality and none of
them earned it:

- **Local accounts** add a user table (a fifth durable category against
  [ADR-0001](../../ARCHITECTURE.md)'s four), password hashing, session
  invalidation, lockout and reset — a security surface this tree would then
  own and have to keep correct, to distinguish one operator from themselves.
- **OIDC** needs a registered client, a redirect URI and a stable reachable
  URL, none of which a loopback default has, in exchange for identity that is
  meaningful only where an organisation already exists.
- **A trusted proxy header** is the cheapest of the three and still requires
  the operator to run and correctly configure a second piece of
  infrastructure, whose misconfiguration is a total bypass rather than a
  degradation.

Adding any of them would have been building the machinery for a question the
deployment does not ask.

## Decision

**There is no user identity model, and there will not be one at this scale.**

Authentication is not deferred, not a TODO, and not waiting on a decision. It
is out of scope, and the three consequences below are the decision, not
side-effects of it.

### 1. `actor` is `"anonymous"`, permanently

Not a placeholder. `CommandRequest.actor` records that this platform does not
know who asked and has decided not to find out, which is a true statement and
therefore a better audit record than a name it invented. The client still
cannot supply it — an attacker-chosen name beside a real operation is worse
than no attribution, because it looks like evidence
(`test_the_client_cannot_choose_who_the_audit_log_blames`).

The audit log is unaffected in the half that matters. It answers *what was
attempted, against what, when, by which path, and whether it worked* across
restarts, and that is the question asked during an incident. "Who" is the
question asked during a dispute between colleagues, and there are none.

### 2. `CommandKind` stays reversible-lifecycle-only, permanently

Start, stop, restart, pause, unpause, kill. **No `remove`, no `prune`, no
volume deletion, no image cleanup — not now and not later.**

[ADR-0012](0012-operations-and-audit.md) made destructive verbs conditional on
answering *who*. This ADR decides that question will not be answered, so the
condition is never met and the conclusion is permanent rather than pending.
That is the trade being made deliberately and in the right direction: the
platform gives up the ability to delete things in exchange for not needing to
know who is deleting them.

This is also what makes the decision safe. A control plane with no
authentication and no destructive verbs has a worst case of *"someone who can
reach the port restarted a container"* — recoverable, visible in the audit
log, and observable in the topology. The same control plane with `prune` has a
worst case of unrecoverable data loss with no attribution. Those are not the
same risk, and only one of them is acceptable without a login.

Anyone who wants the destructive verbs is asking for a different product than
this ADR describes, and their first step is superseding it — not adding a verb
to the enum.

### 3. The trust boundary is the network and the two listeners

This is not "no security". It is security located somewhere other than a user
table, and the controls are already built:

| Surface | Control |
|---|---|
| Browser API | `api.host` is `127.0.0.1` by default. Reaching it from another machine is an explicit, deliberate configuration change. |
| Agent listener | Mutual TLS against an internal CA, single-use join tokens carrying the CA's fingerprint, and per-host operator approval ([ADR-0011](0011-agent-trust-and-enrollment.md)). Unchanged and still the strong boundary. |
| Local agent socket | `0600` in a `0700` directory. The file mode *is* the authentication, and `bystack.conformance.local` checks it. |
| Every mutation | `read_only: true` by default. The platform refuses to change anything until an operator opts out in configuration. |

**The one thing an operator must understand:** setting `api.host` to
`0.0.0.0` so the UI is reachable from another machine means anyone who can
reach that port can start, stop, restart and kill containers on every managed
host. There is no second gate behind it. That is the deployment this ADR
assumes and the reason the port is not bound that way by default. Put it
behind a reverse proxy with authentication if the network is not trusted — the
browser port was deliberately kept able to sit behind an ordinary one
([ADR-0011](0011-agent-trust-and-enrollment.md), the two-listener split), and
this ADR keeps that property available without requiring it.

## Consequences

**Removed from the roadmap:** login, sessions, tokens, a user model, RBAC, and
the destructive verbs that were waiting behind them. `docs/OPEN-WORK.md` §4 is
closed rather than carried.

**Kept:** the audit log stays durable and stays the record of what was
attempted. Its value never depended on the `actor` field, which was
`"anonymous"` for the whole of its existence.

**Not paid:** a user table, a password reset flow, a session store, an OIDC
client registration, and a permission model with one subject in it.

## What would reopen this

This ADR is scoped to a deployment, so it is the deployment changing that
supersedes it — not a feature request. Any of these:

- **More than one operator whose actions must be distinguished.** A shared
  homelab, a small team, anyone who has to answer "was that you?".
- **The Controller reachable from an untrusted network**, where the network
  boundary stops being a boundary.
- **A destructive operation becoming genuinely necessary** — at which point
  the sequencing from ADR-0012 applies again in full and unchanged: identity
  first, then RBAC, then the verb. In that order and not before.

Until one of those is true, adding authentication would be building the
machinery for a question nobody is asking, and adding a destructive verb
without it would be the one sequencing mistake this codebase has been careful
to avoid.
