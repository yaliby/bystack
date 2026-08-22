# ADR-0019 — Deploying agents from the dashboard: a credential that exists for one command

**Status:** Accepted, and built.
**Date:** 2026-08-22
**Depends on:** [ADR-0008](0008-controller-agent-topology.md),
[ADR-0011](0011-agent-trust-and-enrollment.md),
[ADR-0014](0014-no-user-identity.md)

---

## Context

Adding a host is one command, and the dashboard composes it with the token
already in it. The operator's job is to carry that command to the machine and
paste it as root.

That is a good design for one host and a bad one for ten. It is also the last
step in the product that is *manual by construction*: the Controller installs
itself ([ADR-0018](0018-controller-self-update.md)), upgrades itself, upgrades
every agent in the fleet without anybody logging in ([ADR-0017](0017-agent-upgrade-signed-push.md))
— and then the first install on each host is an ssh session and a paste.

The obvious fix is for the Controller to do it: take an address and a root
credential, install the agent, move on. Which runs directly into the decision
this project's whole shape comes from.

### What ADR-0008 deleted, and why this is not that

ADR-0008 removed an agentless design in which the Controller held an SSH key
for every host and reached in over it. It lists four costs, and the second one
is the one that matters here:

> **Credential blast radius.** The Controller held an SSH key granting
> root-equivalent access to every host, because Docker socket access *is* root.
> One compromised Controller was total fleet compromise, with credentials
> sitting on disk to make it convenient.

Read carefully, that paragraph condemns a specific arrangement: **credentials
sitting on disk**, and a Controller that can reach into a host **at will**. It
is not an argument that SSH must never be used; it is an argument about what
the Controller is allowed to *keep*.

The other three costs are about the steady state — connectivity, the event
stream crossing the network, N tunnels held open — and none of them is touched
by using SSH once and hanging up.

## Decision

**The Controller may open an SSH connection to install an agent, and may not
keep anything that would let it open a second one.**

The credential is a parameter of one request. It lives in the process for the
length of one install, is never written to disk, never logged, never returned
by any route, and is dropped when the run ends. There is no host inventory,
no stored key, no "reconnect" button, and no field anywhere that could hold
one.

What that buys is the thing ADR-0008 actually wanted: **a Controller that is
compromised tomorrow gains nothing.** There is no credential to find. The
attacker gets what ADR-0008 already priced — the ability to withhold or
misreport, and no ability to run code on a host, because the agent verifies
what it is asked to install against a key the Controller does not have
([ADR-0017](0017-agent-upgrade-signed-push.md)).

### The steady state is unchanged, and that is the whole claim

After the install, this feature is not in the picture. The agent dials out, the
stream is one long-lived outbound connection, the host listens on nothing, and
the Controller cannot reach it. Every sentence in ADR-0008 §"What gets better"
still holds, with one exception, stated plainly:

**A host must be reachable on SSH at install time.** ADR-0008's proudest claim
is that a host behind NAT is exactly as manageable as one in a datacentre —
and for a host this cannot reach, that claim now has a footnote. So the pasted
command **does not go away and is not deprecated**. It is offered beside this,
in the same dialog, because it is the answer for the NAT case, the
air-gapped case, and the operator who would simply rather not type a password
into a browser.

Two ways in, and neither is the fallback: one is for hosts you can reach, one
is for hosts you cannot.

### One host at a time, which is also what makes it attributable

The run is sequential — each host is installed and confirmed before the next is
touched, and a failure stops the run. The same shape as ADR-0017's staged
rollout, for the same reason: applying a change to ten machines at once is
worth having only in proportion to the confidence that it works, and the first
host is the cheapest place to find out it does not.

It has a second consequence that is not cosmetic. **Nothing in the enrolment
record says which token was redeemed**, so there is no field that maps a new
agent back to the machine the operator typed. What makes the mapping sound is
that only one install is ever in flight: an enrolment that appears during this
host's window is this host's. Deploying in parallel would need a token→host
link in the CA — a new field on a record that is a trust artifact — to answer a
question that sequencing answers for free.

### The host key is trusted on first use, and said so

The Controller has no way to know a host's key in advance; nobody does, on a
first connection. So a first connection records the key and shows its
fingerprint in the result, and every later connection to that address requires
the same key or refuses.

The record lives in `<state_dir>/known_hosts`, beside the CA — the directory
that is already the one worth backing up, already `0700`, already this
account's. It holds public keys and nothing else: it is a record of what was
seen, not a credential, and losing it costs one re-acceptance per host.

**A mismatch is a hard refusal with the two fingerprints printed**, not a
prompt. The prompt is what makes host-key checking theatre everywhere else, and
the thing being installed is a root daemon.

### Nothing the host has to fetch

The Controller uploads what it needs over the same connection: the installer
script, and the agent binary when it holds one for that architecture — a signed
release out of `releases_dir` first, its own bundled copy second. The installer
verifies a signed one against a key compiled into itself, exactly as it does
for a download, and reports an unsigned one as unsigned.

Only when the Controller holds no agent for that architecture does the host
fetch one from GitHub, which is what the pasted command has always done.

This is not an optimisation. "Your servers stay closed" is a claim this product
makes on its front page, and a deployment path that silently required outbound
internet on every managed host would quietly stop it being true.

### `read_only` refuses it

The platform's safe mode exists for a deployment whose operator wants to watch
rather than touch. Installing a root daemon on a machine that did not have one
is the largest thing this Controller can be asked to do to somebody else's
computer, so it is refused with a sentence, the same way updating itself is
(ADR-0018).

### It is not a `CommandKind`, and does not cross ADR-0014

Deploying an agent is not an operation on managed infrastructure — it is what
makes a machine managed in the first place. It gets no `CommandKind`, travels
on no agent connection, and is served only on the browser-facing port, beside
enrollment and the fleet rollout. Nothing an agent sends can reach it.

## Where it lives

| The decision above | The code |
|---|---|
| SSH as a port, with asyncssh at the edge | `backend/.../infra/ssh.py` |
| Trust on first use, and the refusal on mismatch | `backend/.../infra/ssh.py`, `KnownHosts` |
| One at a time; what a run is, and what it forgets | `backend/.../runtime/deploy.py` |
| The credential's whole lifetime | `backend/.../runtime/deploy.py`, `Credential` |
| The operator surface | `backend/.../api/routes/deploy.py` |
| Both ways in, in one dialog | `frontend/src/features/hosts/ui/AddHostDialog.tsx` |

## What this deliberately does not do

- **No stored credential, in any form.** Not encrypted at rest, not in a
  keyring, not "for the session". There is no code path that writes one.
- **No host inventory.** The fleet is the list of agents that dialled in. A
  second list of machines the Controller believes exist is a second source of
  truth about what the fleet is, and ADR-0001's rule is that topology is
  discovered rather than declared.
- **No re-deploy, repair, or remote restart button.** All three need a
  credential the Controller does not have. What exists instead is the signed
  push upgrade, which needs no credential at all.
- **No parallelism.** See above; it is what makes attribution sound.
- **No SSH into anything that is already an agent.** Once a host is enrolled,
  every operation on it goes down the agent's own connection under ADR-0014's
  rules.
- **No prompt on a host key mismatch.**

## Consequences

**The Controller now makes an outbound SSH connection, which it did not
before.** It is bounded to the moment an operator asked for it, to an address
they typed, with a credential they supplied and that nothing keeps. The unit
gains no new capability, no new port and no listener; `asyncssh` is a pure
Python wheel and does not change how the Controller is packaged.

**A root credential now transits the browser-facing API.** That API defaults to
loopback and has no login, which is ADR-0014's deliberate position — so this
is one more reason the default bind matters, and the dialog says so where an
operator will read it. Over `ssh -L`, which is what INSTALL.md recommends, the
credential crosses the network inside the operator's own SSH session.

**The password is in the browser's memory while it is typed.** The field is
never persisted, never put in a URL, and dies with the dialog — the same
discipline the join token already has. This is the honest limit of a web form,
and the key-based path exists for operators who would rather it were not a
password at all.

**A host behind NAT is one paste behind a host that is not.** Stated above,
and it is the only property of ADR-0008 this weakens.

**Two install paths now exist for the first agent on a host, permanently.**
Same shape as the two upgrade paths ADR-0017 left, and handled the same way:
the dialog offers both and says which is which, rather than assuming.
