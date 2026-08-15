# Changelog

What changed, and — where it matters — what you have to do about it. Upgrading
is [INSTALL.md § Upgrading](INSTALL.md#upgrading).

Versions are one number for everything published together: the Controller, the
agent, the wheel and the image all carry it, because the "add a host" command
the dashboard hands out is composed from the Controller's own version and has
to point at a release where the agent binary exists.

---

## v0.3.0

**Upgrade the Controller. The agents can follow whenever it suits you.**
Nothing in this release is in the agent — its binary is the same software
rebuilt under a new number. But one number covers everything published
together, so hosts left on v0.2.0 report themselves as behind the moment the
Controller moves, and the dashboard hands out the upgrade line for them. That
report is right about the number and misleading about the software: there is
no feature on the far side of it. Upgrade the fleet to quiet the report, not
to make anything here work.

### Watch one thing on many hosts at once ([ADR-0016](docs/adr/0016-watched-units-and-processes.md))

Adding a watch now asks which *other* hosts should get it. **Hosts → a server →
Services & processes → Add systemd unit** offers every connected machine
beside the one you started from, and the panel afterwards says *also chosen on
3 other hosts* on the entries that have siblings.

What that does is add the same watch to each host you ticked. It is worth
being plain about what it does not do, because the word "group" invites the
other reading:

- **The group is a label on N ordinary entries, not a rule over them.** Each
  host holds its own entry and can lose it alone; there is no operation that
  edits "the group", and nothing anywhere stores one. A group that owned its
  members would be a second answer to "what is this machine watching", and the
  machine would eventually hold entries nobody chose for it.
- **One host's refusal does not unwind the others.** A duplicate or a bad
  pattern is reported in that host's own words, beside the ones that took it —
  which is why the request answers `200` with a row per host rather than
  `201`. Up to 256 hosts in one act of selection, the fleet-wide counterpart
  of the 64 entries one machine will hold.

### Operating the selection from one button

With something watched on several hosts selected, the operations bar offers
the scope: **this host**, the hosts that share the selection, or all of them.
Each runs as if you had clicked that host's own card — same read-only refusal,
same audit — and **each host gets its own audit entry**, because these are N
operations on N machines and one record claiming the group was restarted hides
which machine actually took it. Nine successes read as success; eight and one
sleeping machine reads as a failure.

The scope resets to this host every time the selection moves. A scope that
persisted is how somebody restarts nine machines meaning to restart one.

### Fixed

- **A stop arriving while the Controller was still starting killed it instead
  of stopping it.** `systemctl stop` during startup — and each half of a
  `systemctl restart` — reported the unit as killed by a signal, so a stop
  that had done exactly what was asked of it read as a failure to the operator
  and to anything watching unit state. For the moment between opening the
  local agent's socket and taking ownership of SIGTERM, the signal met its
  default disposition: the process died where it stood, and the shutdown it
  runs on the way out never ran. Both signals are owned from before the
  Controller opens anything now, and one that arrives during startup is
  carried out as soon as there is something to carry it out on — startup
  finishes, then shutdown runs down the ordinary path. One Ctrl-C still stops
  all three listeners together and a second one still forces. The
  `local-agent.sock` left in the state directory goes with it; that was the
  visible trace rather than the damage, and it never blocked the next start,
  because a Controller unlinks a stale one before it binds.

- **The inspector printed Docker's JSON at you.** A container's published
  ports read `{"public":443,"private":443}` under Attributes, while the card
  and the wire beside it said `:443 → 443`. The inspector now uses the same
  words as the map it is describing.

---

## v0.2.0

**Upgrade the Controller *and* every agent.** This release is mostly in the
agent — a Controller on v0.2.0 talking to a v0.1.0 agent works, and shows that
host no services and no processes, because the old binary cannot see them. It
says so rather than leaving a grey card: adding a watch on a host whose agent
is too old answers with *"this agent could not be told; it may be disconnected
or too old to watch units and processes"*.

### Services and processes on the map ([ADR-0016](docs/adr/0016-watched-units-and-processes.md))

systemd units and running processes now appear beside containers, with the same
start / stop / restart buttons. **Hosts → a server → Services & processes**
picks what a host reports; whatever you select is drawn on the map.

Two decisions shape the whole feature and are worth knowing before you use it:

- **You choose what is watched.** A machine has a couple of thousand units and
  processes and a map with all of them on it is not a map. Only the handful you
  name are collected, so watching costs nothing on hosts you never asked about.
- **A watch on something that does not exist yet shows as *not installed*.**
  That is the point. Silently showing nothing would look exactly like a watch
  you forgot to add.

Watching works with no setup. *Operating* a unit or signalling a process needs
a grant on that host, and both are opt-in files in [`packaging/`](packaging/):
`polkit/49-bystack-agent.rules` to manage units, and
`systemd/bystack-agent-host.conf` to see and signal other users' processes.
Without them nothing is hidden and nothing fails quietly — the buttons report
systemd's own refusal.

No new verb was added to do any of this. `start`, `stop`, `restart`, `kill`,
`pause` and `unpause` are the same six reversible transitions containers
already had; nothing here enables, disables, masks or writes a unit file.

### Upgrading is handed to you rather than remembered

The Controller now composes the upgrade command itself, the way it already
composed the install command, and both the dashboard and `bystack-ctl hosts`
show it where they report that a host is behind. It carries the version the
Controller is actually running, so upgrading the Controller changes the line
without anyone editing a document.

**It has no token, and that is the whole point of it existing.** The obvious
guess — that upgrading is the install command again — costs an afternoon: a
token beside a stored certificate makes the agent enrol a second time, so the
host returns as a stranger awaiting approval while the one you had goes quiet.

### Fixed

- **A stopped service kept offering `Start` after it was running again.** The
  logical `stack` and `service` nodes carried no state at all, so their content
  hash never changed, so the dashboard never refetched the actions for them.
  Both now carry a state folded from every container that realizes them, and
  the card reads that state rather than whichever replica happened to be kept
  last — which is what stops a green dot from sitting beside an inspector panel
  that disagrees with it.
- **`install-agent.sh` no longer forgets how a host was installed.** Re-running
  it rewrote `agent.env` from its arguments, so upgrading a host that was
  installed `--read-only` would quietly hand it back the ability to mutate.
  An absent flag on a host that already has an agent now means "leave it as it
  was", and what it carried over is printed.

### Installing and diagnosing

- **An empty map now explains itself.** `GET /api/v1/healthz` answers with
  every provider's state and the graph's size, so `"providers":[]` with
  `"node_count":0` names the Docker socket group as the cause instead of
  leaving you with a container that is healthy and a dashboard that is blank.
- INSTALL.md and the install page say which of the two numbers in
  `ssh -L 8080:127.0.0.1:8000` belongs to which machine, and why
  `ExitOnForwardFailure=yes` is not optional dressing.
- **The install page covers upgrading**, which it did not: the order, the
  `--build` that decides whether `docker compose up -d` upgrades anything at
  all, the `git stash` that Step 3's own edit to a tracked file makes necessary,
  and the per-server line without a token. The "add a server" command is a real
  pinned URL there rather than `https://.../install-agent.sh`, marked as an
  example so nobody pastes it instead of the one the dashboard composes.
- **The uninstall line assumed a file that is not on the host.** Both documents
  said `sudo sh install-agent.sh --uninstall`, on a machine where the installer
  arrived through a pipe and was never written down. It is fetched the same way
  it was the first time.

---

## v0.1.0

First release with an install path: static musl agent binaries per
architecture, a container image, a wheel carrying both the agent and the
dashboard, systemd units, and `bystack-ctl`. Agents dial in to the Controller
over a mutually-authenticated connection and enrol with a single-use token;
nothing is opened on a managed host.
