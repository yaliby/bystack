#!/usr/bin/env bash
#
# The two places a release signing key is written must name the same keys.
#
#     scripts/check-release-keys.sh
#
# `agent/keys/*.pub` is compiled into the agent and decides which pushed
# binaries a host will run. `RELEASE_KEYS_PEM` in `scripts/install-agent.sh` is
# the same key in the encoding openssl wants, and decides which downloaded
# binaries a host will install. They are edited by hand, in two files, in two
# encodings, and nothing in the build reads one to produce the other.
#
# ## Why a mismatch is worth a CI job
#
# It does not fail where it is made. A host installs fine -- the installer is
# checking against its own copy -- and then refuses every upgrade the
# Controller pushes for the rest of its life, with a message about trust. The
# operator is looking at a signature failure on a healthy fleet and has no
# reason to suspect two constants in the repository disagree.
#
# The reverse is worse and quieter: a key dropped from the installer but left
# in `agent/keys/` is a fleet that upgrades happily and cannot be *installed*
# on a new host, which surfaces weeks later as one machine that will not join.
#
# Both are one diff away at every rotation, because a rotation edits both files
# and there is no shape of the edit that fails on its own.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# What the agent trusts: hex, one key per line, `#` comments -- the same
# parsing `agent/build.rs` does, because that file is the authority on what a
# built binary ends up holding and this has to agree with it rather than with
# the README beside it.
committed="$(
	cat "${REPO}"/agent/keys/*.pub 2>/dev/null |
		sed 's/#.*//' | tr -d '[:blank:]' | grep -v '^$' | sort || true
)"

# What the installer trusts, asked of the installer rather than parsed out of
# it. `--keys` decodes the PEM through the same openssl the verification path
# uses, so this compares the value that would actually be *used* and not a
# string that happens to sit near it in the file.
installed="$(
	sh "${REPO}/scripts/install-agent.sh" --keys 2>/dev/null |
		grep -Eo '^  [0-9a-f]{64}$' | tr -d '[:blank:]' | sort || true
)"

if [ -z "$committed" ] && [ -z "$installed" ]; then
	echo "no release signing keys anywhere: agents cannot be upgraded over the wire,"
	echo "and downloads are checked against SHA256SUMS only. That is what a checkout"
	echo "without a key looks like and it is a legitimate state (ADR-0017)."
	exit 0
fi

if [ "$committed" = "$installed" ]; then
	echo "release signing keys agree ($(echo "$committed" | grep -c .)):"
	# shellcheck disable=SC2001  # per-line indent; ${//} has no ^ anchor
	echo "$committed" | sed 's/^/  /'
	exit 0
fi

echo "release signing keys disagree." >&2
echo >&2
echo "  agent/keys/*.pub -- what a built agent will accept a pushed release from:" >&2
# shellcheck disable=SC2001
echo "${committed:-    (none)}" | sed 's/^/    /' >&2
echo >&2
echo "  install-agent.sh RELEASE_KEYS_PEM -- what a first install will accept:" >&2
# shellcheck disable=SC2001
echo "${installed:-    (none)}" | sed 's/^/    /' >&2
echo >&2
# shellcheck disable=SC2016  # the backticks are prose, not a substitution
echo '  Both are edited by hand at every rotation. `scripts/sign-agent.py keygen`' >&2
echo '  prints each key in both encodings; a key belongs in both files for at' >&2
echo '  least the release that introduces it.' >&2
exit 1
