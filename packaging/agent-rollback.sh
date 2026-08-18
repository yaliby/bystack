#!/bin/sh
#
# Undo an upgrade that never reached a Controller (ADR-0017).
#
# Installed at /usr/local/lib/bystack/agent-rollback.sh and run by
# `bystack-agent-rollback.service`: at boot, and once a minute while an upgrade
# is on probation.
#
# ## Why this is a shell script and not a subcommand of the agent
#
# Everything else on this path is `bystack-agent apply-update`, because
# verifying an ed25519 signature in POSIX sh is not a thing to attempt. This
# half needs no cryptography at all -- it puts a file back -- and it must work
# **when the newly installed agent does not**. That is the entire case it
# exists for. A rollback that ran the binary under test would be a rollback
# that fails exactly when it is needed.
#
# ## What decides
#
# The updater wrote a probation flag naming the version it installed and a
# deadline. The agent writes, on every accepted `HelloAck`, a file naming the
# version that connected and when. Probation is satisfied when those agree:
# the version just installed reached a Controller *after* it was installed.
#
# "The process is up" is not the claim being tested. A binary that starts,
# fails to parse a frame and reconnects forever is exactly the failure this
# exists for, and systemd would report it as `active (running)` throughout.
#
# The agent's file is a claim by an unprivileged process and is treated as one:
# it can only ever *end* probation, it names a version so a stale marker from
# the previous binary cannot pass for a fresh one, and the record of what to
# roll back to lives in root's directory where the daemon cannot reach it.
#
# POSIX sh, no bashisms: this runs on whatever a stranger's NAS has.
#
set -eu

UPDATE_DIR=/var/lib/bystack-agent-update
PROBATION="${UPDATE_DIR}/probation"
STATE_DIR="${BYSTACK_STATE_DIR:-/var/lib/bystack-agent}"
CONNECTED="${STATE_DIR}/connected"
TIMER=bystack-agent-rollback.timer
SERVICE=bystack-agent.service

say() { echo "bystack-agent-rollback: $*"; }

# `key=value`, last one wins. Both files are written by us.
field() { sed -n "s/^$1=//p" "$2" 2>/dev/null | tail -n 1; }

disarm() { systemctl stop "$TIMER" >/dev/null 2>&1 || true; }

if [ ! -f "$PROBATION" ]; then
	# Nothing in flight. The overwhelmingly common case, and the reason this
	# is cheap enough to run at every boot.
	disarm
	exit 0
fi

version="$(field version "$PROBATION")"
installed_at="$(field installed_at "$PROBATION")"
deadline="$(field deadline "$PROBATION")"
binary="$(field binary "$PROBATION")"
previous="$(field previous "$PROBATION")"
now="$(date +%s)"

# A probation file we cannot read is worse than none: it would roll the host
# back on a schedule with no way to satisfy it. Removing it leaves the new
# binary in place, which is the state the machine is already in.
if [ -z "$version" ] || [ -z "$deadline" ] || [ -z "$binary" ]; then
	say "${PROBATION} is unreadable; clearing it and leaving ${binary:-the agent} in place"
	rm -f "$PROBATION"
	disarm
	exit 0
fi

if [ -f "$CONNECTED" ]; then
	seen="$(field version "$CONNECTED")"
	seen_at="$(field unix "$CONNECTED")"
	if [ "$seen" = "$version" ] && [ "${seen_at:-0}" -ge "${installed_at:-0}" ]; then
		say "${version} reached the Controller; upgrade confirmed"
		rm -f "$PROBATION"
		disarm
		exit 0
	fi
fi

if [ "$now" -lt "$deadline" ]; then
	# Still inside the window. Starting an already-running timer is a no-op;
	# this is what re-arms after a reboot, where the boot run of the service
	# is the only thing that fires.
	systemctl start "$TIMER" >/dev/null 2>&1 || true
	exit 0
fi

# -- the deadline passed ----------------------------------------------------

say "${version} has been installed since ${installed_at} and has not reached a Controller"

if [ -n "$previous" ] && [ -f "$previous" ]; then
	# Into the target directory, then renamed within it. A file created
	# somewhere else and moved here keeps the SELinux type of where it was
	# made, and the service then fails to start in a way that looks like a bad
	# binary rather than a bad label.
	staged="$(dirname "$binary")/.bystack-agent.rollback.$$"
	cp -p "$previous" "$staged"
	chmod 0755 "$staged"
	mv -f "$staged" "$binary"
	if command -v restorecon >/dev/null 2>&1; then
		restorecon -F "$binary" || true
	fi

	# Written before the restart, so the reason survives whatever the restart
	# does. It is also the only place an operator finds out this happened
	# without reading the journal.
	cat >"${UPDATE_DIR}/last-rollback" <<EOF
# Written by agent-rollback.sh.
rolled_back_from=${version}
at=${now}
reason=it did not reach a Controller within the probation window
EOF
	rm -f "$PROBATION"
	disarm

	say "restored ${previous} and restarting ${SERVICE}"
	systemctl restart "$SERVICE" || true
	exit 0
fi

# Nothing to go back to: a first install, or a `.prev` somebody removed. Saying
# so plainly beats a loop that reports a rollback it cannot perform once a
# minute forever.
say "no ${previous:-previous binary} to restore. Leaving ${version} in place; this host needs a look."
cat >"${UPDATE_DIR}/last-rollback" <<EOF
# Written by agent-rollback.sh.
rolled_back_from=${version}
at=${now}
reason=it did not reach a Controller, and there was no previous binary to restore
EOF
rm -f "$PROBATION"
disarm
