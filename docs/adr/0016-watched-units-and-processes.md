# ADR-0016 — Watching units and processes: selection, not discovery

**Status:** accepted
**Date:** 2026-08-11
**Supersedes:** nothing. **Amends** the hardening in ADR-0015's unit, by
adding two opt-in grants that live in a drop-in rather than in the base unit.

## Context

Everything ByStack manages today runs in a container, and plenty of what an
operator actually cares about does not: the reverse proxy that is a systemd
service, the database that predates the fleet, the worker somebody starts from
a shell script. The map is a control surface for one half of a machine and
silent about the other, and the half it is silent about is where the incidents
that are hardest to see come from.

The obvious version of this feature is the wrong one, and it is wrong in a way
this project has already written down. A machine has two thousand systemd
units and several hundred processes. Discovering all of them is what
ARCHITECTURE §12 calls the second-rate cAdvisor: a process sitting next to a
socket, enumerating everything, on every managed host, forever — for a picture
nobody wanted, that buries the eight things they did.

## Decision

**The membership of the unit and process slices is the operator's selection,
and nothing else.** A watch list per host, held on the Controller, pushed to
the agent, and reported back as two ordinary slices over the existing
authoritative-delta protocol.

Six consequences, each of which is the decision rather than an implementation
detail:

### 1. The list is configuration, and therefore durable

ADR-0001 permits durable storage for four categories; this is the first —
*user intent, not discoverable*. The graph rebuilds itself from the fleet
within seconds of a cold start and this cannot, because nothing out there
knows what somebody chose. `infra/watch/durable.py`, one JSON file,
write-and-rename, bounded at 64 entries per host.

The bound is the retention policy clause 4 asks for, in the form configuration
takes: a ceiling rather than a TTL. An age-based rule would be actively wrong
— a watch on a service that has run untouched for a year is the *most* settled
entry in the file.

### 2. Identity for a process is the rule, never the pid

The two-layer identity of ADR-0002, arrived at from the other end. A pid is
worse than a container id: recycled by the kernel, and gone on precisely the
event somebody is watching for. So the entity is the *watch rule* — "the thing
whose executable is `/usr/local/bin/mydaemon`" — its matches are attributes of
it, and a rule matching nothing is an observation (`absent`) rather than an
absence.

This is also what makes the lifecycle path safe. A command names a watch id;
the agent resolves it to pids *at the moment it signals them*, in one worker,
with no `await` in between. A pid resolved a minute ago and signalled now is
`SIGKILL` to whatever inherited the number.

### 3. A watched thing that is not there is still a node

`load_state: not-found` for a unit that is not installed; `absent` for a rule
matching nothing. Both are answers. The alternative — dropping the entity —
produces a card that silently never appears, which an operator reads as *I
never added it* rather than as *it is not installed*, and that is the failure
this feature exists to avoid rather than one to introduce.

It is also what makes "add one by name" work: an operator can watch a service
before deploying it, and the map says what it is waiting for.

### 4. No new verb, and therefore no collision with ADR-0014

`start`/`stop`/`restart` are systemd's own words, `kill` is `KillUnit` and
`SIGKILL`, and `pause`/`unpause` are `SIGSTOP`/`SIGCONT`. Every one of them
maps onto a `CommandKind` that already existed, so the closed reversible set
stays closed and ADR-0014's pairing — no user identity, no destructive verb —
is untouched.

Three things are deliberately absent for the same reason:

- **`enable` / `disable` / `mask`.** Reversible, but not *lifecycle*: they
  change what the machine does after the next reboot, which is not something
  the graph going back to how it was would undo.
- **Writing unit files from the Controller.** That is provisioning, and with
  no user identity in front of the API it is arbitrary code execution as root
  on every managed host for anyone who can reach the port.
- **`start` for a bare process.** There is no recorded way to launch one, and
  the only way to offer the button would be to hold a command line for the
  agent to execute — the same thing in a smaller box. A process watch can stop
  something; starting it belongs to whatever supervises it, and if the answer
  is *nothing does*, the honest fix is a unit file.

### 5. The agent speaks D-Bus itself

The same call `Cargo.toml` records for `hyper`, made in the other direction.
What systemd asks of a client is a fixed-width header, a type-length-value
body and a four-line SASL exchange; `zbus` would bring a dependency tree
measured against a binary that is measured. `agent/src/dbus.rs` is the client,
complete on the read side — a parser that could not represent one of the values
`GetAll` returns could not *skip past* it either, and every value after it
would be read at the wrong offset.

Measured cost of the whole feature: **1.82 → 1.96 MiB** binary (budget 12) and
**3.95 → 5.00 MiB** RSS (budget 20).

### 6. One poll, and it is admitted

Units are event-driven: `Subscribe` plus a `PropertiesChanged` match, coalesced
into the same 250 ms window Docker's events use, so a `systemctl restart`
reaches the map in under two seconds. Processes have no event stream available
at these privileges — the kernel's is netlink, behind `CAP_NET_ADMIN` and an
address family the unit does not permit — so they are re-read every ten
seconds, and only when something is watched.

ARCHITECTURE §11's preference order is satisfied rather than broken: native
event stream, push, incremental sync, *periodic reconcile*. The first three are
unavailable here, and the fourth is what is left.

## The privilege question, and where the boundary actually is

Reading unit state needs nothing: any local user may call `GetAll` on a unit.
So **watching works on a stock install** — the map draws every selected
service, live, with no grant of any kind.

Operating them does not. systemd answers an unprivileged `RestartUnit` with
`InteractiveAuthorizationRequired`, which the Controller shows verbatim. Two
opt-in files close that, and both are opt-in because §11 asks whether a
capability can be optional and here it can:

| File | Grants | Needed for |
|---|---|---|
| `packaging/polkit/49-bystack-agent.rules` | `manage-units` for the agent's account, on any unit | Operating services |
| `packaging/systemd/bystack-agent-host.conf` | `ProtectProc=default`, `CAP_KILL` | Watching and signalling processes |

**polkit cannot scope the first to the watch list, and the agent can.** A rule
is fixed on disk and evaluated per call; the list is chosen in a browser and
changes while the daemon runs. A rule that named units would need rewriting on
every edit, and one that matched a prefix would be a naming convention
pretending to be a boundary. So the grant is broad and the *bound* lives in
`agent/src/host.rs`, which refuses a lifecycle command for any unit that is not
on this host's list before it touches the bus — the second choke point
ARCHITECTURE §9 already requires for containers, doing the same job here.

That is a real trade and it is stated rather than smoothed over: a compromised
agent with this rule installed can restart any unit on its host. It could
already stop any container, which on most machines is the larger power.

## Consequences

- Two node kinds (`unit`, `process`), one edge kind (`runs_in`), two slices,
  three frames (`WatchList`, `InventoryRequest`, `InventoryResponse`). The
  `.proto` grew; no tag was renumbered or reused.
- A host that watches nothing is unchanged in every measurable way: no bus
  connection, no `/proc` walk, no frames. The feature costs what it is used
  for.
- `Command.target_kind` now says which namespace a target lives in. Empty means
  `container`, which is what every Controller before this sent — so an old
  frame and a new agent agree without a version check.
- The `units` capability answers "can this be done *here*", not only "was this
  compiled in". A machine with no systemd says so at `Hello`, and the operator
  finds out while they are still looking at the box they typed the name into.
- **What would reopen this:** a supervisor worth integrating with directly
  (a process manager with its own API), or a second operator — at which point
  ADR-0014 is being reopened anyway, and `enable`/`disable` become answerable.
