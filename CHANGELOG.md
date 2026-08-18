# Changelog

What changed, and — where it matters — what you have to do about it. Upgrading
is [INSTALL.md § Upgrading](INSTALL.md#upgrading).

Versions are one number for everything published together: the Controller, the
agent, the wheel and the image all carry it, because the "add a host" command
the dashboard hands out is composed from the Controller's own version and has
to point at a release where the agent binary exists.

---

## v0.4.0

### Upgrade the fleet from the dashboard ([ADR-0017](docs/adr/0017-agent-upgrade-signed-push.md))

**Hosts → Upgrade them to ‹version›.** The Controller pushes a signed release
down the connection each agent already holds; the host verifies it against a
key compiled into the agent, installs it itself, and puts the old binary back
if the new one cannot reach the Controller within ten minutes.

It runs **one host at a time**, confirmed by that host coming back and saying
what it is now, and a failure stops the run rather than completing it. That is
the entire trade this feature makes: applying a change to two hundred machines
at once is worth having only in proportion to the confidence that the change is
good, and the first host to run a release is the cheapest place to discover it
is not. `bystack-ctl upgrade --watch` is the same run without a browser.

**The Controller is a distribution channel and is not trusted with content.**
It has no signing key. A Controller in an attacker's hands can withhold an
upgrade, send an old one, or send nothing — and cannot produce a binary any
agent will run. That is what makes *where the artifacts came from* an
operational question instead of a trust decision, and it is why the daemon that
receives a binary never gains write access to `/usr/local/bin`: it stages inside
the directory it already owns, and a `.path` unit hands the install to a
`oneshot` with no network.

### What you have to do to turn it on

Nothing, to keep working as you are: with no release in the Controller's
`releases` directory, hosts are upgraded by `install-agent.sh` exactly as
before, and the dashboard hands out that command as it always did.

Releases from this one on are signed. `agent/keys/release.pub` is the key every
agent built from this tree accepts a pushed binary from, and the same key is in
`RELEASE_KEYS_PEM` at the top of `scripts/install-agent.sh`, so the *first*
install on a host is verified on the same terms as every upgrade after it —
against a signature made offline, rather than against a `SHA256SUMS` fetched
over the same connection as the binary it describes. `install-agent.sh --keys`
prints what a given copy of the installer will accept.

To hand releases out, put the artifacts and their manifests where the
Controller can see them:

    cp dist/bystack-agent-* /var/lib/bystack/releases/

Running your own fleet from a fork is the same three steps it always was — mint
a key, put its public half in `agent/keys/`, its PEM in the installer — and
`scripts/release-agent.sh <tag>` does the signing, refusing to sign a version
the binary does not report, a key the fleet does not trust, or one architecture
without the other. The private key belongs on the machine that makes releases
and nowhere else: not on the Controller, and not in CI, where a workflow change
signs anything.

### Fixed: the installer reported every successful enrolment as a failure

`install-agent.sh` decided whether a host had enrolled by looking for `*.pem` in
the state directory. The agent writes `agent.crt`, `agent.key` and `ca.crt`, so
the check never matched: a good install printed "has not enrolled yet" and
exited non-zero, and — the part that was not cosmetic — **left the single-use
join token in `/etc/bystack/agent.env`**.

A token sitting beside a stored certificate makes the agent re-enrol on its
next start, deliberately, and a single-use token is refused the second time. So
the host worked until something restarted it, and then went quiet. A reboot did
it; so did the upgrade above, which always restarts the agent — and the
rollback would put the previous binary back into the same wall.

If a host of yours has a `BYSTACK_TOKEN=` line in `/etc/bystack/agent.env` and
a certificate in `/var/lib/bystack-agent`, delete the line and restart the
agent. Running this release's installer over it does the same thing.

**Every host needs `install-agent.sh` run on it once more**, and only once. An
agent from v0.3.0 or earlier has no updater unit to trigger, so it cannot be
pushed to; the panel and `bystack-ctl hosts` count those hosts separately and
print the command for them. The upgrade after that one is the button.

### Rotating the signing key

The agent holds a *set*. Version *N* is signed by the old key and ships
`{old, new}`; version *N+1* is signed by the new key and ships `{new}`. Two
ordinary upgrades retire a key, which is why a leak is answered by a release
rather than by visiting every machine in the fleet.

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

- **The fleet panel called a newer agent an older one.** A host running ahead
  of the Controller — upgraded before it, which the supported order does not
  ask for but nothing prevents — was counted with the hosts behind it and then
  described as "running an older agent". The comparison behind that notice
  never took a view on which number was larger; only the name of the count and
  the sentence it fed did. Both cases want the same command anyway, since
  `install-agent.sh` installs the version this Controller is composed from in
  whichever direction that moves the host. The notice now says those hosts are
  not on the Controller's version and names the version, and the card sets the
  two numbers beside each other with no adjective in between.

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
