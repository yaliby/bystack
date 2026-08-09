# ADR-0012 — Operations: lifecycle only, logical targets, no optimistic updates

**Status:** Accepted
**Relates to:** [ADR-0001](../../ARCHITECTURE.md) (durable categories),
[ADR-0003](../../ARCHITECTURE.md) (provider partitions),
[ADR-0008](0008-controller-agent-topology.md) (Controller/Agent split)

---

## Context

Everything built until now discovers, correlates and displays. None of it
*acts*. That is the difference between a control plane and a very good
diagram, and the gap was closing on its own in the worst possible way: a
restart button is six lines against the Engine API, and six lines is exactly
short enough to be written in the route handler by whoever needs it first.

The decisions below are the ones that are expensive to reverse afterwards.

## Decision

### 1. One choke point, above the routes

Every operation passes through `CommandService`. The REST route parses and
delegates; it decides nothing. Read-only enforcement, target resolution,
fan-out, the target limit and audit all live in the service.

This costs one indirection today, when there is exactly one caller. It buys
the property that the *second* caller — a scheduler, a webhook, an
alert-driven remediation, the agent command path from
[ADR-0009](0009-agent-wire-protocol.md) — cannot be written in a way that
forgets a check. A guarantee implemented in one route is not a guarantee, it
is a habit.

Read-only is checked **first**, before the target is even resolved. A
read-only control plane must not be usable as an oracle for which URNs exist,
and putting the cheapest check first is also what makes it obvious when
someone reorders the block for readability.

### 2. Commands never mutate the graph

A `restart` returns without a single node having changed. The graph moves
when the provider's watch observes the transition and publishes a delta.

The cost is real and visible: for a second or two after a successful command
the UI still shows the old state. The alternative — optimistically writing
`restarting` into the store — buys that second back and hands over a control
plane that displays states the infrastructure never reported. Every optimistic
write is a claim we cannot verify, and the cases where it is wrong (the
command succeeded but the container immediately died, the daemon accepted and
then failed) are precisely the incidents the tool exists for.

So the gap is made explicit instead of hidden. The UI captures the target's
content revision at the moment the engine answers and shows
*"Applied · waiting for discovery to confirm"* until that revision changes.
If it never changes, it says so — which is a genuine diagnostic, because it
means the host's event stream is gone.

### 3. Targets are URNs, including logical ones

An operator restarts *the `web` service*, not `container 3f2a…`. The
Controller expands a service to the containers realizing it, and a stack
through its services, by walking the same `contains` / `realized_by` edges the
mapper declared.

This is the payoff for the two-layer identity in ADR-0002 and it belongs in
the Controller for the same reason mapping does: only the Controller holds the
graph. A provider is never handed a URN it cannot act on directly.

Expansion is **not** filtered by state. If a stack contains a stopped
container, `stop` is dispatched to it too and the engine answers `304`, which
we record as `noop`. Filtering in the service would create a second policy
that can disagree with the engine, and skipping targets silently is how a
"successful" stack restart leaves half a stack down.

### 4. Reversible lifecycle transitions only

`start`, `stop`, `restart`, `pause`, `unpause`, `kill`. No `remove`, no
`prune`, no volume or image deletion.

Not because they are hard — each is one more HTTP call — but because of what
they require. A destructive operation is only defensible if the platform can
answer *who deleted the database volume, and when*.

Destructive operations, durable audit and authentication ship together or not
at all. Shipping the fun third of that is how a platform acquires a
capability it cannot account for.

**Amended: the audit is now durable** (`infra/audit/durable.py`, §4a below).
That answers *what* and *when*, across restarts. It does not answer *who* —
nothing authenticates a browser to this API, so every entry is still
attributed to `anonymous` — and the clause above is unchanged in effect: two
of the three are done, so nothing destructive appears yet.

### 4a. The audit log is a JSON Lines file, appended, bounded by count

The enrollment registry (ADR-0011) is the precedent: a table's shape, in a
file, because Postgres is not in this tree and moving it is then a change to
one module. One thing differs and it changes the format.

The registry is a few hundred bytes rewritten wholesale when a human approves
a host. An audit log is append-heavy — two writes per operation, §5 — so it is
**appended, never rewritten in place**, and `finalize` writes a second record
for the same id with the later one winning on read. That is what lets a
process killed mid-operation leave a complete attempt on disk rather than a
truncated file.

Retention is **by count**, not by age. "Ninety days" is what an auditor asks
for and the wrong thing to implement first: it makes the file's size a
function of how busy the installation is, which is the unbounded growth
ADR-0006 clause 4 exists to prevent. A count is a bound; a duration is a hope.
The file is compacted, write-and-rename, when it grows past twice the
retention — so compaction is amortised and a Controller killed during one
comes back to the previous complete file.

Durable is the **default**, and the in-memory ring survives as the fallback
for a Controller with nowhere to write. "Cannot persist" and "will not start"
are different answers and only one of them is acceptable here: a full disk
must not stop an operator restarting the service that filled it. Both the
per-record write and the fallback are logged at ERROR, so the gap is findable.

### 4b. The write is fsynced, and it is not on the event loop

Every record is fsynced before `record` returns, because the entry this cannot
afford to lose is the one written immediately before a command that then hung
the process — buffering loses exactly that one. But `CommandService.execute`
is a coroutine, so doing it inline stopped every agent's pump and every
browser's delta stream for the duration, twice per command. Measured at
0.01 ms on tmpfs, 0.78 ms on NVMe, and tens of milliseconds on a spinning
disk, a busy host or network storage.

So `AuditLog.record` and `finalize` are **async**, and the durable
implementation awaits `asyncio.to_thread` around the file work. Async on the
*port*, not just the one implementation: a `record` whose sync-ness depended
on which log the composition root chose would put that choice into the shape
of every caller, and every implementation worth having beyond these two has
I/O in it.

Deliberately **not** a write-behind queue. That needs a flush on shutdown, an
ordering guarantee and a test for a crash mid-queue, and it buys latency
nobody here is short of by trading away durability-at-return, which is the
whole point. One lock serialises the writes so the file's order is the order
the operations happened in, and compaction is handed a snapshot taken on the
event loop — a worker thread must never iterate a mapping `record` can still
add to.

The defect was invisible to the test suite by construction: `tmp_path` is
tmpfs. `test_audit.py` now slows `os.fsync` on purpose and asserts the longest
gap between two turns of the event loop, which is the only way to see it.

### 5. Audit is written before dispatch, and includes refusals

The entry is recorded when the command is authorized and finalized when it
returns. A command that hangs, or a process that dies mid-operation, still
leaves evidence that it was attempted — and "what was tried" is the only
question an audit log is ever asked during an incident.

Refusals are recorded too. *"The restart did not happen because the platform
is read-only"* is the single most useful line the log can contain during the
post-mortem of an outage a restart would have ended.

The client cannot supply the actor. An attacker-chosen name sitting next to a
real operation is worse than no attribution, because it looks like evidence.

This clause originally ended "the field appears when authentication does,
populated from the session." **[ADR-0014](0014-no-user-identity.md) decided
authentication does not arrive**: at one operator on their own network there
is nobody to distinguish them from. `actor` is therefore `"anonymous"`
permanently, and the destructive verbs §1 held back are held back permanently
rather than pending — the condition they were waiting on is never met, which
is the whole reason it is safe to have no authentication at all.

### 6. Provider capability is a protocol, not a flag

A provider that implements `supported_commands` and `execute` is writable; one
that does not is read-only. There is no registration step and no
`can_execute = False` to remember.

Prometheus, Grafana and every other observational provider are therefore
read-only by construction — which is the correct default, obtained by writing
nothing. The partition rule from ADR-0003 extends unchanged: a node is only
ever operated on by the provider that discovered it, because `node.source` is
what selects the executor.

### 7. Which commands apply is decided server-side

`unpause` applies to paused containers, `start` to stopped ones. That policy
lives in the Docker provider, which owns the state vocabulary, and the UI
fetches it from `GET /commands/actions`.

A copy of it in TypeScript would be correct the day it was written and wrong
by the release that adds a state — and wrong in the direction of offering
buttons that cannot work. The same endpoint reports *why* there are no
actions, so read-only mode and a disconnected host are explained rather than
rendering as an inexplicably empty panel.

A disconnected provider reports no available commands at all. During an
outage "restart it" is exactly what an operator will try, so the answer needs
to be honest before the click rather than after a slow timeout.

## Consequences

- The UI has a visible lag between a command succeeding and the topology
  reflecting it. This is deliberate, labelled, and the price of never
  displaying an unverified state.
- ~~The audit trail does not survive a restart.~~ It does, as of §4a.
- ~~What remains missing is attribution, and that is now the constraint
  holding §1's destructive verbs.~~ Attribution is not missing, it is
  **declined**: [ADR-0014](0014-no-user-identity.md) decides this platform has
  no user identity, so every entry reads `anonymous` permanently and the verbs
  §1 held back are held back permanently. The constraint did not get resolved;
  it got made unconditional, which is what makes the log complete for the set
  of reversible verbs it actually records.
- The audit log is a file on the Controller's disk, so it is one more thing
  in `agents.state_dir` worth backing up, and one more thing that grows.
  Bounded at 20,000 operations (~8 MB) by default.
- One in-flight limit (`MAX_TARGETS = 64`) refuses rather than truncates. A
  partially-applied stack operation is the worst outcome available here.
- `timeout=0` is a real request — `docker stop -t 0` — and is read with
  `is None`, not truthiness. Read with `or`, the one operator who explicitly
  asked for no grace period silently gets ten seconds.

## Alternatives considered

**Accept-then-poll (`202` plus a job id).** Rejected. These operations take
seconds and the operator is watching. It would add a job store, a status
endpoint and a class of orphaned-job bugs to buy nothing. The operation that
genuinely needs asynchrony — a scheduled or fleet-wide rollout — is a
different feature with a different shape, and it can have its own.

**Optimistic graph updates with reconciliation.** Rejected; see §2. The
failure mode is silent and appears only during incidents.

**Executing commands over a fresh connection per request.** Rejected. It
would open a second SSH tunnel per host at the moment an operator is already
waiting, to reach a daemon the informer is demonstrably already connected to.
Commands borrow the informer's client; when there is none, the command is
cleanly refused rather than dialling out mid-incident.
