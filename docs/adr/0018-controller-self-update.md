# ADR-0018 — The Controller updates itself: the same signatures, pulled instead of pushed

**Status:** Accepted, and built.
**Date:** 2026-08-21
**Extends:** [ADR-0017](0017-agent-upgrade-signed-push.md)
**Depends on:** [ADR-0008](0008-controller-agent-topology.md),
[ADR-0014](0014-no-user-identity.md)

---

## Context

[ADR-0017](0017-agent-upgrade-signed-push.md) made upgrading a fleet a button.
The Controller distributes a signed release, each host verifies it against a
key the Controller does not have, and a host that comes up unable to reach a
Controller puts its previous binary back by itself. Two hundred machines, one
click, and a rollback that does not need anybody to be awake.

It left one machine out, and it had to. **The Controller has no parent to push
to it.** Everything ADR-0017 built depends on there being a distributor on the
other end of a connection the host already holds, and for the Controller there
is nothing there. So the machine whose entire purpose is to make ssh
unnecessary elsewhere is upgraded by ssh: a `pip install --upgrade` into a
venv, a `systemctl restart`, and a hope.

That is worse than it sounds, for three reasons that are all about this
machine specifically:

1. **It is the one that cannot be rolled back by anything.** A managed host
   that comes up broken is undone by a timer ADR-0017 installed on it. A
   Controller that comes up broken has taken the rollback machinery for every
   other machine down with it.
2. **`pip install --upgrade` is not atomic and not offline.** It needs the
   network at the worst possible moment, it half-applies when it fails, and
   there is no inode to put back. "Undo that" is not an operation a virtualenv
   supports.
3. **It is the reason a fleet stays behind.** The dashboard offers a fleet
   upgrade only for releases the Controller holds, and the Controller only ever
   holds releases somebody copied onto it. In practice that means the fleet is
   upgraded when the operator upgrades the Controller — which they do rarely,
   because it is the scary one.

The question this record answers is not "how do we download a file". It is
**how much new trust machinery a self-update is allowed to introduce**, and the
answer this ADR commits to is *none*.

## Decision

**Reuse ADR-0017's contract exactly. Add a local transport and nothing else.**

Same signing key, same manifest format, same signing script, same version
ordering, same anti-downgrade rule, same probation-then-rollback shape. The
only field that differs anywhere in the system is `name`:
`bystack-controller` instead of `bystack-agent`.

### The trust contract is not extended, it is linked

`agent/release/` is a crate — `bystack-release` — holding the manifest parser,
the compiled-in key set, the ed25519 check and the version ordering. The agent
linked it by moving that code out of `agent/src/upgrade.rs`; the manager links
the same crate.

This is the part that would have gone wrong if it had been written twice, and
it would have gone wrong *quietly*. A second parser is a second opinion about
whether an unknown key is an error. A second key set is a second place a
rotation has to land, and a rotation that reaches one component and not the
other is a fleet that upgrades while its Controller refuses to, with a message
about trust. A second version comparator is a second answer to whether `0.10.0`
outranks `0.9.0`.

One crate, one `cargo test`, one lockfile — so the agent and the manager
verify with the identical `ring`, and neither can be refactored past the other.

**`name` is why this is safe at all.** ADR-0017 put it inside the signed
document on the grounds that "a key that ever signs a second artifact must not
let one be presented as the other". This is that second artifact. Without the
field, a genuine, current, correctly signed Controller could be handed to a
managed host as its agent — signature valid, digest matching, version higher,
architecture right — and the only thing refusing it is one string comparison.
`bystack-release`'s `check_target` takes the expected name as an argument, and
both call sites pass a constant.

### Pull, because there is nobody to push

The manager fetches from GitHub Releases. That is the whole of what is new
about the transport, and ADR-0017 already priced it:

> With verification at the agent, provenance no longer depends on how the
> Controller obtained the bytes. Fetching from GitHub Releases, shipping inside
> the wheel, or an operator dropping files in a directory become equivalent.

The same sentence applies here with the same force, and it is what lets the
transport be **`curl`**. Not an HTTP client, a TLS stack and a root certificate
store compiled into a binary that runs as root — three dependencies bought to
protect bytes that are refused unless they are signed. A hostile mirror, a
mis-issued certificate and an operator behind a proxy are the same event: bytes
arrive, and they are not executed.

What *is* pinned is what curl may do. HTTPS only, including after a redirect
(`--proto` and `--proto-redir`, and the second one matters — without it a `302`
to plaintext is a download this would happily make). A size cap, a timeout, and
a URL composed from a repository compiled into the binary and a string that has
been checked for being a version. `BYSTACK_RELEASE_BASE` names a mirror for an
air-gapped install and may be plaintext, and *only* a URL under that mirror may
be: an operator who pointed this at a host on their own network has made a
decision this binary is in no position to second-guess, and no redirect or
composed path can arrive at plaintext any other way.

### The Controller becomes one file

`scripts/build-controller.sh` produces `bystack-controller-<arch>`: a zipapp
carrying the Controller, its dependencies, the dashboard and the agent for that
architecture.

This is not packaging preference. It is what makes the update **atomic and
undoable**, which is the property the whole rollback argument rests on. A
`rename` within one directory replaces an inode; the previous one is kept as a
hard link beside it, and putting it back is another `rename`. A virtualenv
supports neither operation, so a design built on one could promise to *attempt*
a rollback and not to perform one.

It also makes the Controller the same *kind of thing* as the agent, which is
what let the signing tool, the manifest format and the key set cover both
without an argument.

**The cost is a pinned interpreter, and it is stated rather than hidden.**
Wheels with compiled extensions are built for one CPython ABI, so a release is
assembled for one minor version — the floor `backend/pyproject.toml` already
declares. A host without that interpreter cannot run the artifact, and the
manager finds that out *before it stops anything* (below). Bundling an
interpreter with PyInstaller removes the constraint and costs a native build
per architecture, an emulated aarch64 release job and a second packaging format
to keep working; worth revisiting when the constraint bites, not worth paying
up front.

### One folder, and one exception to it

Everything lives under `/opt/bystack`: the Controller, the manager, the two
files they talk through, the fleet's artifacts, root's probation record.

The agent scatters by necessity — its binary belongs in `/usr/local/bin`
because that is where a host's PATH looks, and root's probation flag has to
live somewhere the daemon cannot write. The Controller has neither constraint.
It is one deployment on one machine, and an operator who wants it gone should
be able to delete a directory.

The exception is `ipc/`, which is `root:bystack`, mode **`1770`**. The
Controller runs as `bystack` and must be able to create `update.intent`; the
sticky bit is what stops it also being able to unlink `update.status`, which
root owns. That matters because the status file is the one thing in this
feature the Controller cannot say for itself — and the dashboard's progress bar
is drawn from it.

`bystack-controller.service` gains `ReadWritePaths=/opt/bystack/ipc
/opt/bystack/cache` and nothing else. `ProtectSystem=strict` is unchanged, and
neither directory holds an executable.

### Files, not a socket

A socket needs a listener, and the listener would have to be the root side:
a resident root daemon accepting connections and parsing frames for the 99.99%
of its life when nobody is updating anything. What is actually communicated
here is one sentence a few times a year.

So: `update.intent` is written by the Controller, `bystack-manager.path` fires
on it existing, and `update.status` is written by root as the run proceeds.
Both documents are spelled the way the signed manifest is — a magic line, then
`key value` — and parsed the same strict way at both ends, with a test that
reads the magic out of the Rust source and compares it to the Python constant.

**The intent is trusted for exactly one thing: that somebody asked.** The
version in it is validated as a version and then used to compose a URL under a
compiled-in repository; what comes back is refused unless it is signed by a key
compiled into the manager and names a version higher than what is installed.
The health URL in it is required to be loopback, because a probation check that
can be pointed at another machine is one that always passes.

So the worst a compromised Controller can do through this channel is cause its
own host to install a genuine, current, correctly signed Controller. That is
the property ADR-0017 gives the Controller over the fleet, pointed the other
way.

### Probation collapses into the process that made the change

ADR-0017 needs an independent timer, and it explains at length why: the agent
is on probation until *a Controller somewhere else* accepts its `Hello`, which
is evidence arriving on a connection the updater does not have.

The Controller's evidence is a GET to loopback. The process that made the swap
can simply make the request — so there is no timer, no second unit watching a
flag, and **no resident root daemon**. `bystack-manager.service` is
`Type=oneshot`: fetch, verify, stop, swap, start, poll `/healthz` until it
reports the version that was installed, and exit.

"The port answers" is not the claim being tested. The old Controller that never
actually stopped answers; a proxy in front of it answers. **The version is part
of the question**, and it is the only thing that establishes that the file which
was installed is the file that is serving.

What this does not cover is the process dying mid-probation — a reboot, an OOM
kill, a `Ctrl-C` on a manual run. That leaves an armed record and a Controller
nobody is watching, so `bystack-manager-rollback.service` runs at boot with
`ConditionPathExists=` on the probation file. One unit, which does not start at
all on an ordinary boot, rather than a timer that ticks forever.

### The failure that can be found in advance is found in advance

Before anything is stopped, the manager runs the downloaded artifact's
`--version` and requires it to print what the signed manifest says.

This is the check that pays for the pinned interpreter. A host with no
`python3.12` passes every other check in the sequence — the signature holds,
the digest matches, the version is higher, the architecture is right — and then
takes a minute of downtime to discover it by rollback. Two seconds while
everything is still running gets the same answer with a sentence instead.

**The rollback is for the failures that cannot be found in advance.** Using it
for one that can is how a safety mechanism becomes the normal path.

### Phase two: the fleet, using the rollout that already exists

When the manager has a healthy new Controller, it downloads the matching signed
agents for **both** architectures into `/opt/bystack/releases/` and names the
version in the status file's `cascade` field. The new Controller comes up, sees
a cascade for its own version that it has not run, and starts ADR-0017's staged
rollout.

Nothing about the rollout changes. One host at a time, each confirmed before
the next is touched, a failure stops the run, and the progress view is the
version chip on each card. This ADR adds a *reason to start one*, not a second
way to run one.

The condition is deliberately narrow:

- `cascade` must equal this Controller's own version. A cascade naming anything
  else is a report about a machine in a different state — an update that rolled
  back, or a status file left from last time.
- it must not already have been run, recorded on the Controller's side because
  the status file is root's and outlives every restart.

**Dropping artifacts into the release directory does not cascade.** An operator
who copies files in by hand gets what they always got: a release the dashboard
offers and a button. Only an update *this machine performed* starts a fleet-wide
change without being asked, because only then is there evidence about what just
changed and why.

### The dashboard is served by the process being replaced

Which means the one thing this UI has to be correct about is **its own backend
going away mid-operation**. Between `applying` and the next successful poll,
every request from the page fails, and none of those failures is an error —
they are the update working.

`useController` swallows them and leaves the last answer on screen, which
correctly reads as "still swapping". A hook that set an `unreachable` flag would
replace a progress bar with an error at the exact moment the operator most needs
to be told to wait, and the likely response to that error is an ssh session to a
machine that was going to be fine in forty seconds.

This is also the only progress bar in the product, and it earns the exception.
Everywhere else progress is real state changing — a version chip moving on a
card. Here, for part of the operation, there is genuinely nothing to poll. The
percentage is invented on the client and stated as such, so it cannot become a
second model of the update that disagrees with the first.

There is no cancel button. Once the swap has started, the process that would
have to honour the request is the one being replaced, and the machine already
undoes a swap that does not come up.

## Where it lives

| The decision above | The code |
|---|---|
| The contract is linked, not extended | `agent/release/` |
| The key set, generated once for both | `agent/release/build.rs`, `agent/keys/` |
| `name` is what keeps the two artifacts apart | `agent/release/src/lib.rs`, `check_target` |
| Pull, over curl, HTTPS-pinned | `agent/manager/src/shell.rs` |
| Files, not a socket; what the intent is trusted for | `agent/manager/src/ipc.rs` |
| One folder, and the `1770` | `agent/manager/src/layout.rs` |
| Verify, smoke-test, swap, probation, rollback | `agent/manager/src/apply.rs` |
| The path unit, the oneshot, the boot-time undo | `packaging/systemd/bystack-manager{.path,.service}`, `bystack-manager-rollback.service` |
| The Controller as one file | `scripts/build-controller.sh` |
| One key, two artifacts | `scripts/sign-agent.py`, `scripts/release-agent.sh` |
| The Controller's end of the two files | `backend/.../infra/manager.py` |
| Phase two, and the one condition it fires under | `backend/.../runtime/selfupdate.py` |
| The operator surface | `backend/.../api/routes/controller.py` |
| A page that survives its own backend | `frontend/src/features/hosts/model/useController.ts` |

Two of them are checked across the language boundary rather than by comment.
`test_wire.py` now reads `MAGIC`, `AGENT` and `CONTROLLER` out of
`agent/release/src/lib.rs` and requires `sign-agent.py` to be able to produce
both names; `test_manager.py` reads the intent and status magic out of
`agent/manager/src/ipc.rs`, and checks that every phase the dashboard treats as
terminal is one the manager actually writes.

## What this deliberately does not do

- **No second key, no second manifest format, no second signing tool.** If any
  of those had been needed, the design was wrong.
- **No new listener and no new port.** One outbound connection to a compiled-in
  repository, and one to loopback.
- **No arbitrary execution.** The intent names a version. It cannot name a URL,
  a path, a command or a repository.
- **No downgrade.** The floor is what `bystack-controller --version` reports,
  read from the file that actually runs. Rollback is the operation that goes
  backwards, and it is local and automatic.
- **No resident root daemon.**
- **No change to the fleet rollout.** Phase two starts the run ADR-0017 already
  defined and adds no policy to it.
- **No new `CommandKind`, and nothing crossing ADR-0014's boundary.** Updating
  the Controller is not an operation on managed infrastructure.

## Consequences

**A root process on the Controller's host now has network access, and that is
a real weakening.** ADR-0017's proudest structural property is that the only
process able to write `/usr/local/bin` has `PrivateNetwork=yes`. That split is
available precisely because the agent has a distributor; the Controller does
not, so the component that pulls and the component that installs are the same
one. What bounds it: it has no listener, it makes one outbound connection to a
compiled-in host, it is `Type=oneshot` so it is not resident, it can write only
inside `/opt/bystack`, and it executes nothing it downloads without a signature
from a key it was built with. It is a smaller surface than the Controller
beside it — and unlike that one, it is not on the network at all except during
an update somebody asked for.

**The signing key gets more valuable again.** ADR-0017 said it becomes the most
valuable secret in the project. It now also signs the artifact that replaces the
control plane itself, so a leak is a fleet *and* its Controller. The mitigation
is the one already chosen and unchanged: the key is offline, on the machine that
cuts releases, not in CI, and rotation is two ordinary releases.

**A release is now four artifacts per architecture, and a partial one is worse
than none.** A tag with signed agents and an unsigned Controller is a fleet that
can be upgraded and a Controller that will not update itself; the reverse is a
Controller that updates and then has nothing to hand the fleet. `release.yml`
refuses to publish unless every one is present, and `release-agent.sh` refuses
to attach a partial set.

**The Controller is pinned to one CPython minor version per release.** Stated
above, checked before the swap, and the thing to revisit first if this becomes
awkward.

**`bystack-manager` is not itself updated by any of this.** It is installed by
the same hands that install the Controller and upgraded by replacing the file —
the same position `install-agent.sh` holds for the agent, and for the same
reason: something has to be the root of the chain. It is small, it changes
rarely, and it is the piece whose correctness cannot be delegated to itself.

**The container install gets none of this, and says so.** There is no single
file to rename inside an image, and an image is upgraded by pulling a new one.
`GET /controller` answers `updatable: false` with the sentence that names which
install path this is, rather than showing a button that would write a file
nothing reads.

**Two upgrade paths remain live for the Controller, permanently.** A venv
install and a checkout are both legitimate and neither can be updated this way.
That is the same shape ADR-0017 left the fleet in, and it is handled the same
way: the dashboard says which one you are on rather than assuming.
