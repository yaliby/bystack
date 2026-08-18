# ADR-0017 — Controller-driven agent upgrade: the Controller distributes, the host verifies

**Status:** Accepted, and built.
**Date:** 2026-08-17
**Supersedes:** [ADR-0015](0015-agent-upgrade.md)
**Depends on:** [ADR-0008](0008-controller-agent-topology.md),
[ADR-0009](0009-agent-wire-protocol.md),
[ADR-0011](0011-agent-trust-and-enrollment.md)

---

## Context

[ADR-0015](0015-agent-upgrade.md) considered three shapes for upgrading a
fleet, chose the second as the right one if this were ever automated, and then
declined to automate it. Its reason was one cost, stated precisely:

> It collides with the hardening. `packaging/systemd/bystack-agent.service`
> sets `ProtectSystem=strict`, so `/usr/local/bin` is read-only to the service.
> An agent that replaces its own executable needs that relaxed —
> `ReadWritePaths=/usr/local/bin` — which grants a network-facing daemon write
> access to a directory of executables that other things run.

That paragraph is still true about everything it describes. What it contains is
an assumption that is not: **that the process which receives the binary is the
process which installs it.** Nothing requires that. Split the two, and the cost
the ADR called blocking is not paid at all — the daemon writes only inside the
`StateDirectory` it already owns, and the base unit does not change by a line.

ADR-0015 said the right way to revisit this was a supersession rather than a
flag, on the grounds that relaxing `ProtectSystem=strict` should not arrive as
an implementation detail. This is that supersession, and it arrives with the
relaxation removed rather than justified.

Its other two conclusions survive intact and are not reopened here. **Shape 1
stays rejected**: nothing below gives the agent a root store or an HTTP client,
and its network surface remains one pinned connection to one CA. **The
reporting half stays as built**: `agent_version` on every `Hello`, skew per host
in `GET /agents`, `bystack-ctl hosts` and the Hosts panel.

What ADR-0015 stopped short of, and therefore never had to price, is the two
questions that decide whether this feature is safe to have at all:

1. What makes a binary that arrived over the wire worth executing?
2. What happens when it is worth executing and is still the wrong binary?

The first is a trust question and has a clean answer. The second is an
availability question, it is the one that can take a fleet off the map, and it
is the larger part of this record.

## Decision

**Push the binary down the existing stream. Verify it on the host against a key
the Controller does not have. Install it from a process that is not on the
network.**

### The Controller is a distribution channel and is not trusted with content

It holds releases, it picks the artifact matching each host's architecture, and
it sends. It has no signing key, so a Controller in an attacker's hands can
withhold an upgrade, send an old one, or send nothing — and cannot produce a
binary any agent will run.

This dissolves an objection ADR-0015 raised and could only park. It observed
that a Controller upgrading a mixed fleet must fetch artifacts for
architectures it is not, and called that *"moved rather than removed"* — the
internet download relocated to the machine that already faces the internet.
With verification at the agent, provenance no longer depends on how the
Controller obtained the bytes. Fetching from GitHub Releases, shipping inside
the wheel, or an operator dropping files in a directory become equivalent, and
the choice becomes an operational convenience instead of a trust decision.

### What is signed is a manifest, not a hash

The signature covers a manifest — `{name, version, arch, sha256, released_at}`
— and the binary is accepted because its digest matches the `sha256` inside
that signed document.

Signing the digest alone is the version of this that looks equivalent and is
not. The version has to come from somewhere, and if it comes from the wire it
is the attacker's claim about a genuinely signed artifact: an old release with
a known weakness, announced as new. The anti-downgrade check below would be
comparing against a number the sender chose. `arch` is in there for the same
reason at a smaller scale, and `name` because a key that ever signs a second
artifact must not let one be presented as the other.

### The public key is compiled into the agent, and rotation is a release

Embedded in the Rust binary at build time — not a file placed on the host at
install.

A key on disk is worth what the path is worth. Inside the daemon's
`StateDirectory` it is writable by the process it is supposed to constrain;
anywhere else it is a file that arrived over the same channel as the binary it
verifies, which adds nothing to that channel. Compiling it in makes the trust
descend from the initial install, and makes each version carry the key set for
the next one.

So the agent holds a **set** of accepted keys, not one key. That is the entire
rotation mechanism and it is worth spelling out, because a single permanent key
with no path to replace it means a leak is resolved by visiting every machine
in the fleet by hand:

- version *N* is signed by the old key and trusts `{old, new}`;
- version *N+1* is signed by the new key and trusts `{new}`.

Two ordinary upgrades retire a key. A compromised key is revoked by a release.

**Where the private key lives is part of this decision.** Holding it in CI
secrets makes the CI account the most valuable target in the project — a
workflow change signs anything. At the release cadence this project actually
has, signing offline on the maintainer's machine is both simpler and stronger,
and it is what this ADR recommends. If it is ever moved into CI, it belongs in
a signing job separate from the build job, behind a protected environment with
a required review.

### The version floor is the binary already on the disk

No stored floor, no state file, no counter. The updater runs as root, reads the
version of `/usr/local/bin/bystack-agent`, and refuses a manifest that is not
higher.

Any floor written to a file is a floor that something can lower, and the
obvious place to write it — the daemon's own state directory — is writable by
exactly the process this check exists to survive. It would also be a second
source of truth about which version is installed, able to disagree with the
file that actually runs.

**Downgrade is therefore not an operation.** Rollback is, and it is local,
automatic, and described below.

### The daemon stages, a path unit triggers, a oneshot installs

Three processes, and the only one with write access to `/usr/local/bin` has no
network.

**The daemon** verifies the signature and the version, then writes the binary
and the manifest into its own `StateDirectory` and writes a trigger file. All
of that is inside `/var/lib/bystack-agent`, which it already owns.
`bystack-agent.service` is unchanged — `ProtectSystem=strict`, empty
`CapabilityBoundingSet`, `SystemCallFilter=@system-service`, all of it.

The daemon's verification is not the one that matters. It is there so a host
refuses a bad artifact at the cheap end, before writing megabytes to a disk
that may be someone's root filesystem, and so the refusal is reported over a
live connection with a reason.

**The trigger is a `.path` unit**, watching for the trigger file, not
`systemctl start bystack-agent-updater.service`.

The agent *can* start units — but only where
`packaging/polkit/49-bystack-agent.rules` is installed, and that file is
deliberately optional (ADR-0016): a host that only watches services does not
have it, and systemd answers with `InteractiveAuthorizationRequired`. Building
the upgrade path on `StartUnit` would make a fleet-wide capability depend on an
unrelated opt-in, and fail on precisely the hosts whose operator chose the
narrower grant. A path unit needs none of it — no polkit, no new D-Bus method,
nothing added to the daemon's syscall surface — and it accepts no arguments, so
there is no shape in which a compromised daemon parameterises what root does.

**The updater** is `Type=oneshot`, `PrivateNetwork=yes`, and the only unit in
the system with `ReadWritePaths=/usr/local/bin`.

### The updater trusts nothing it is handed

It copies the staged binary and manifest into its own `RuntimeDirectory`,
`root:root`, mode `0700`, and does every check there.

Verifying in the staging directory and installing from the staging directory
would be two reads of a path inside a directory owned by `bystack-agent`. A
compromised daemon that wins the race between them has just had root install
its file, and the entire signature chain is bypassed by a few milliseconds. The
copy closes it: after it, the bytes being verified are the bytes being
installed, in a directory the daemon cannot reach.

The signature and the version are then checked again — not as ceremony, but
because this is the first check performed on the copy that will actually be
installed, and the daemon's earlier check was performed on something else.

### The install is a rename inside `/usr/local/bin`, then `restorecon`

A temporary file created **in the target directory**, `fsync`, `rename` over
`bystack-agent`, and `restorecon -F` where SELinux is present. The previous
inode is kept as `bystack-agent.prev`.

The tempting version — stage in `/var/lib`, rename into place — is broken under
enforcing SELinux, and it fails in a way that looks like a bad binary rather
than a bad label: a file renamed across directories keeps `var_lib_t`, and the
service will not start. Creating the file in the target directory has it
inherit that directory's default type, and `restorecon` is cheap insurance for
the case where it does not.

`rename` within one directory is atomic and replaces the inode rather than
writing through it, so a running agent keeps the file it started with. This is
the same property that makes `install` correct in `scripts/install-agent.sh`,
for the same reason.

The updater then runs `systemctl restart bystack-agent.service`. It is a
separate unit in its own cgroup, so restarting the service that triggered it is
an ordinary job rather than a process killing its own parent. The updater unit
takes no ordering relationship to `bystack-agent.service`; the restart is the
whole of the coupling.

### A binary that starts is not an agent that works

This is the part ADR-0015 never reached, and the part that can lose a fleet.

A signed, correct, higher-versioned binary can still be an agent that connects
to nothing — a protocol regression, a panic on an architecture the release was
not tested on, an environment assumption that holds on the maintainer's
machine. The host then drops off the map and the only way back is ssh: the
exact cost this feature exists to avoid, incurred on every host at once,
by one button.

So the swap is on probation:

- the updater writes a probation flag in a root-owned path — **not** the
  daemon's `StateDirectory`, for the same reason the version floor is not
  stored there;
- the new agent clears it only on a `HelloAck` with `accepted=true`. Not on a
  successful start. "The process is up" is not the claim being tested; "the
  Controller is talking to it" is;
- an independent timer restores `bystack-agent.prev` and restarts if the flag
  survives N minutes.

**The timer is independent because `Restart=` cannot do this job.**
`bystack-agent.service` sets `StartLimitIntervalSec=300` and
`StartLimitBurst=5` — a crash-looping agent stops being restarted, which is the
behaviour that unit argues for at length and which must not change to
accommodate this. A rollback that rides on the restart path would either
require weakening that or would never run.

### One click is a staged rollout, not a broadcast

The Controller upgrades one host, waits for its `Hello` to report the new
version, and only then continues; a failure stops the run rather than
completing it.

Simultaneity is the whole of the risk here. Applying a change to two hundred
machines at once is worth having only in proportion to the confidence that the
change is good, and the first host to run a release is the cheapest possible
place to discover it is not.

The surface for this already exists. ADR-0015's skew report — version per host,
already on `GET /agents` and already drawn — is the progress view of a rollout
without becoming a second model of one.

### The wire: a capability and chunks

Gated on a capability advertised in `Hello.capabilities` (field 5). This is the
standing rule for anything added to the wire, and the reason is diagnostic: an
older agent that has never heard of the message must refuse with words rather
than time out.

The transfer is chunked, following the shape `LogsChunk` already established,
in the unused `50` tag block of the envelope's `oneof`.

**Not for memory.** `dist/` is 2.2 MB for x86_64 and 1.8 MB for aarch64 against
`MemoryMax=64M`; a single frame would fit. It is chunked because that socket
also carries the live graph, and a multi-megabyte burst on it is a topology map
that stops moving for the duration of every upgrade. Chunks also make a dropped
connection resumable instead of restarting the transfer.

## Where it lives

| The decision above | The code |
|---|---|
| The wire: a capability and chunks | `proto/bystack/agent/v1/agent.proto`, the `50` block |
| The public key is compiled in | `agent/build.rs`, `agent/keys/` |
| Verify, stage, trigger | `agent/src/upgrade.rs`, `offer` / `chunk` |
| The daemon's `HelloAck` evidence | `agent/src/upgrade.rs`, `note_connected` |
| The updater trusts nothing it is handed | `agent/src/upgrade.rs`, `apply_update` |
| The path unit, the oneshot, the timer | `packaging/systemd/bystack-agent-{update.path,updater.service,rollback.service,rollback.timer}` |
| The rollback | `packaging/agent-rollback.sh` |
| Signing, offline | `scripts/sign-agent.py` |
| The release pipeline, and what it refuses to sign | `scripts/release-agent.sh` |
| The two encodings of the key agree | `scripts/check-release-keys.sh` |
| The Controller distributes and is not trusted | `backend/.../infra/releases.py`, `providers/agent/upgrade.py` |
| One click is a staged rollout | `backend/.../runtime/upgrade.py`, `api/routes/upgrades.py` |
| The skew report is the progress view | `frontend/src/features/hosts/` |

Two of them are checked across the language boundary rather than by comment:
`test_wire.py` reads the manifest magic and the artifact name out of the Rust
source, and `bystack.conformance.fleet` pushes a real signed release to a real
agent binary and requires it to be staged, an older one refused, and one signed
by a key the harness invented refused.

**The key now exists.** `agent/keys/release.pub` holds the public half and
`RELEASE_KEYS_PEM` in `scripts/install-agent.sh` holds the same key in the
encoding openssl reads, so an agent built from a checkout advertises the
`upgrade` capability and a first install is verified against a signature rather
than against a checksum fetched over the same connection as the binary. The
private half was minted by `scripts/sign-agent.py keygen`, lives on the machine
that cuts releases, and is not in this repository, on the Controller, or in CI.

Those two files are edited by hand, in two encodings, and nothing in the build
derives one from the other — so `scripts/check-release-keys.sh` compares them
in CI. The mismatch is worth a job because it does not fail where it is made: a
host installs fine against the installer's own copy and then refuses every
pushed upgrade for the rest of its life, with a message about trust.

A build that trusts nothing is still legitimate and still the behaviour a fork
gets: drop `agent/keys/*.pub`, or set `BYSTACK_RELEASE_KEYS_PEM`, and the agent
advertises no capability and is upgraded by `install-agent.sh` — which is why
the Controller refuses to push to it with a sentence rather than sending two
megabytes.

## What this deliberately does not do

- **No root store and no HTTP client in the agent.** ADR-0015's shape 1 stays
  rejected; its reasoning is untouched and its property is preserved.
- **No relaxation of `bystack-agent.service`.** Not `ReadWritePaths`, not
  anything else. The unit is the same file after this feature as before it.
- **No new port, no new listener, no polkit requirement.**
- **No arbitrary execution.** The oneshot takes no arguments and has one input:
  a signed manifest naming a digest.
- **No unit-file changes and no new `CommandKind`.** Upgrading the agent is not
  a fleet command; it is a distribution the agent applies to itself. ADR-0014's
  boundary is where it was.

## Consequences

**The signing key becomes the most valuable secret in the project.** It was
worth saying that the Controller is no longer trusted with content; the honest
other half is that the trust moved rather than vanished, onto a key whose
handling is now a security property of the product. The key set and the
rotation path above exist so that a leak is a release rather than a fleet-wide
manual visit.

**The Controller must hold artifacts for architectures it is not.** The wheel
carries exactly one, on purpose (`backend/hatch_build.py`). Fetching the others
is now an ordinary operational choice rather than a trust decision, but it is
still work, and a Controller with no route to them can upgrade only the hosts
that match it.

**Tags in the `50` block are spent.** Tags are never renumbered and never
reused; this is the cost ADR-0015 was careful to avoid paying speculatively,
and it is paid deliberately here.

**The mechanism ships as packaging, so the first upgrade is still
`install-agent.sh`.** The updater unit, the path unit, the rollback timer and
the polkit-free trigger are files on the host, installed by the installer. A
host running an agent from before this exists has no path unit to trigger, so
the transition to push-upgrade is one last run of the script per host.
`install-agent.sh` remains load-bearing; it stops being the *routine* path
rather than stopping being a path.

**Signing improves the installer too, and is used there.**
`install-agent.sh` now fetches the manifest and its signature first and falls
back to `SHA256SUMS` only when there is no signature to be had — which is an
integrity check against corruption and not against the origin, and it says so
when it lands there. The same manifest and the same key make the first install
verifiable on the same terms as every upgrade after it, which is where the
trust chain actually begins. `--keys` prints what a given copy of the installer
will accept, because "what does the thing I am about to pipe into root believe
in" should be answerable without reading shell.

It checks the whole document and not just the digest: the magic, so a format it
cannot parse is refused rather than read by position; and `name`, so a key that
ever signs a second artifact cannot have one presented as the other. Neither is
defending against a forgery — the signature is checked first. They defend
against a *genuine* signature over the wrong thing, which is the only attack
left once the key holds and the whole reason what gets signed is a document.

**It costs the agent almost nothing, and that was not luck.** The release
binary went from 1.96 MiB to 2.03 MiB — about 80 KB for the manifest parser,
the staging state machine and the updater. Ed25519 verification and SHA-256 are
free because `ring` is already in the binary for TLS and for the join token's
fingerprint (ADR-0011), and the whole feature adds no dependency. A design that
had needed an HTTP client or a second crypto library would have paid for it
here, on every architecture, on every host in every fleet (ARCHITECTURE §11).

**A host can still end up needing ssh.** Both binaries bad, a full disk, a
machine that will not boot. The rollback covers the failure this feature
introduces, not every failure a host can have — and the skew report names which
host it is, which is the thing that was missing before ADR-0015 and is still
the answer here.
