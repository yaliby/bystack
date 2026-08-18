# Release signing keys

Every `*.pub` file in this directory is compiled into the agent by `build.rs`
and becomes a key the agent will accept a pushed release from (ADR-0017). One
ed25519 public key per line, 64 hex characters, `#` comments ignored.

`release.pub` is the key ByStack's own releases are signed with. An agent built
from this checkout trusts it, advertises the `upgrade` capability, and can be
upgraded from the dashboard. **The private half is not here and is not
anywhere in this repository** — see below.

An *empty* directory is still a legitimate build and is what a fork gets before
it mints its own key: the agent trusts nobody, advertises no capability, and is
upgraded by running `scripts/install-agent.sh`, which is also what every host
does for its first install. That is the honest behaviour for a build that can
verify nothing, and it is why a placeholder key would be worse than none — a
build that looks capable of verifying and is not.

**Whatever is here must also be in `RELEASE_KEYS_PEM` at the top of
`scripts/install-agent.sh`**, in PEM rather than hex. Nothing derives one from
the other, so `scripts/check-release-keys.sh` compares them and CI runs it: a
key in one file and not the other produces a host that installs fine and then
refuses every upgrade for the rest of its life.

## Minting the key a fleet is signed with

    scripts/sign-agent.py keygen --out ~/.bystack/release.key

It writes the private key `0600` and prints the public half. Put that in a file
here — `release.pub` — and commit it. The private key does not go in this
repository, does not go on the Controller, and is not needed by anything except
the machine that signs a release.

**Where it lives is part of the decision.** Holding it in CI secrets makes the
CI account the most valuable target in the project, because a workflow change
signs anything. At this project's release cadence, signing offline on the
maintainer's machine is both simpler and stronger. If it ever moves into CI, it
belongs in a signing job separate from the build job, behind a protected
environment with a required review.

## Cutting a signed release

    scripts/release-agent.sh v0.4.0 --attach

Run on the machine that holds the private key, after the release workflow has
published the binaries. It fetches the exact bytes that were published, signs
them, verifies them twice, and uploads the manifests.

**The signing is deliberately not a CI job**, and the pipeline is split across
that line: `.github/workflows/release.yml` builds and publishes with no key and
nothing to steal, and this runs afterwards by hand. In the window between them
`install-agent.sh` falls back to `SHA256SUMS` and says so, and the Controller
declines to push a release it has no signature for — visible in both places
rather than silently unverified.

What it refuses to sign is the point of having it. Every one of these produces
a *valid signature over the wrong thing*, which nothing downstream can catch:

- a version in the manifest that the binary does not report. The agent's
  anti-downgrade check reads the binary, so a fleet that installed "0.4.0"
  while reporting 0.3.0 refuses the real 0.4.0 forever as not-an-upgrade;
- a signature under a key `agent/keys/` does not hold — artifacts nothing in
  the fleet will install, and this is the only cheap place to notice;
- one architecture without the other, which is a fleet that upgrades on half
  its hosts and refuses on the other half.

## Rotating

The set is what makes this survivable without visiting every machine:

- version *N* is signed by the old key and ships `{old.pub, new.pub}` here;
- version *N+1* is signed by the new key and ships `{new.pub}`.

Two ordinary upgrades retire a key. A compromised key is revoked by a release,
and the fleet is only as reachable as its slowest host is up to date — which is
the reason the second step is a release and not a config change.

## Building against a key that is not committed

    BYSTACK_SIGNING_KEYS=<hex>[,<hex>…] scripts/build-agent.sh

Added to whatever is in this directory rather than replacing it. Useful for a
fork running its own fleet, and for testing the path end to end without
committing anything.
