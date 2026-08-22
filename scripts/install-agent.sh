#!/bin/sh
#
# Put the ByStack agent on this machine.
#
#     curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-agent.sh \
#       | sudo sh -s -- --controller wss://controller:8443 --token bst1.<ca>.<secret>
#
# That is the command the dashboard hands out. Everything below is what it
# does, and it is a short list on purpose: fetch one static binary, write one
# environment file, install one unit, start it. There is no package manager
# involved, no repository to add and no daemon to configure, because the agent
# is one file and its whole configuration is four environment variables.
#
# ## Upgrading a host that already has an agent
#
# The same command with the new tag in the URL and *no* `--token`:
#
#     curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-agent.sh \
#       | sudo sh -s -- --controller wss://controller:8443
#
# Dropping the token is what makes it an upgrade rather than a second host.
# The certificate under /var/lib/bystack-agent stays where it is, so the
# Controller sees the machine it already knows come back with a new version
# rather than a stranger asking to be approved -- and a token sitting beside a
# stored certificate would make the agent re-enrol and mint a fresh identity
# for a host that already has one. The binary is replaced with `install`, which
# swaps the inode, so the running process keeps the file it started with and
# the change takes effect at the restart below.
#
# ## Offline, and on a machine that cannot reach GitHub
#
#     scripts/build-agent.sh                       # on a machine that can
#     scp dist/bystack-agent-x86_64 host:/tmp/
#     sudo sh install-agent.sh --binary /tmp/bystack-agent-x86_64 \
#       --controller wss://controller:8443 --token bst1.…
#
# `--binary` skips the download entirely. It is not a fallback path bolted on
# afterwards -- a fleet on a network with no egress is an ordinary deployment
# for this product, and the agent being a single self-contained file is most of
# the reason it is.
#
# ## After this, upgrades come from the Controller
#
# This script installs four more units and a small script beside the agent
# (ADR-0017), and they are what turns the next upgrade into a button. The
# Controller pushes a signed release down the connection that is already open;
# the agent verifies it against a key compiled into itself, stages it, and a
# path unit hands it to a `oneshot` with no network that does the install and
# puts it on probation.
#
# **So this remains the path for the first install on a host, and stops being
# the routine one.** A machine running an agent from before ADR-0017 has no
# updater unit to trigger, which is why the transition to push-upgrade is one
# last run of this script per host.
#
# ## What it deliberately does not do
#
# It does not install Docker, does not add anyone to the `docker` group, and
# does not touch an existing agent's certificate. The first two are decisions
# about a machine that belong to whoever owns it; the third is this host's
# identity to the Controller (ADR-0011), and quietly replacing it would make a
# reinstall look like a new host.
#
# POSIX sh, not bash: this runs on whatever a stranger's NAS has.
#
set -eu

REPO="${BYSTACK_REPO:-yaliby/bystack}"
VERSION="${BYSTACK_VERSION:-v0.5.0}"

BIN_DIR="${BYSTACK_BIN_DIR:-/usr/local/bin}"
CONF_DIR=/etc/bystack
UNIT_DIR=/etc/systemd/system
UNIT="${UNIT_DIR}/bystack-agent.service"
LIB_DIR=/usr/local/lib/bystack
SERVICE_USER=bystack-agent
STATE_DIR=/var/lib/bystack-agent
#: Root's own directory: the probation flag and the record of what to roll
#: back to. Not the agent's state directory, which is writable by exactly the
#: process the rollback exists to survive (ADR-0017).
UPDATE_DIR=/var/lib/bystack-agent-update

#: Public keys that releases are signed with, PEM, concatenated.
#:
#: A fetched binary is checked against a signature made offline on the machine
#: that cut the release -- which is a statement about *who built this* that a
#: `SHA256SUMS` fetched over the same TLS connection as the binary it describes
#: cannot make. This is where the trust chain begins: the key compiled into the
#: agent this installs is the one every later upgrade is verified against, so
#: whatever this block accepts is what the host will keep accepting.
#:
#: **The same key as `agent/keys/release.pub`**, in the other encoding openssl
#: wants. `scripts/check-release-keys.sh` fails the build if the two disagree,
#: because a mismatch is invisible until it is a host that installed fine and
#: then refused every upgrade after it.
#:
#: More than one is legal and is how a key is rotated: concatenate the PEM
#: blocks, ship a release signed by the new one, then drop the old block.
#: `scripts/sign-agent.py keygen` prints what goes here.
RELEASE_KEYS_PEM="-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAFyeyEKNSP2/z8wQKqd9Fq99J/8NwJvNPyAdD7rTPDvM=
-----END PUBLIC KEY-----"

# Spelled as an assignment and an `if` rather than `${BYSTACK_...:-<block>}`,
# for two reasons that both cost more than the line they save: a `\`-continued
# default inside `${:-}` is a construct shellcheck reads as string replacement
# and a human reads twice, and `[ -n … ] && …` as the last command of a block
# is the `set -e` trap this script warns about further down.
#
# It *replaces* the set rather than adding to it. A fork running its own fleet
# wants its keys and not ours, and "the environment adds to the built-in trust"
# is the one behaviour nobody would want to discover by reading the source.
if [ -n "${BYSTACK_RELEASE_KEYS_PEM:-}" ]; then
	RELEASE_KEYS_PEM="$BYSTACK_RELEASE_KEYS_PEM"
fi

controller=""
token=""
local_binary=""
docker_socket=""
read_only=""

die() {
	echo "install-agent.sh: $*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
usage: install-agent.sh --controller <wss url> [--token <token>] [options]

  --controller URL   Controller base URL, e.g. wss://controller:8443
  --token TOKEN      Join token, for the first install on this host. Mint one
                     in the dashboard under "Add a host".
  --binary PATH      Install this file instead of downloading one
  --socket PATH      Docker socket (default /var/run/docker.sock)
  --read-only        Refuse every mutation, whatever the Controller sends
  --keys             Print the release signing keys this installer trusts,
                     and exit. Worth doing before piping this into root.
  --uninstall        Stop the service and remove everything except the
                     certificate, which is this host's identity

Environment: BYSTACK_REPO, BYSTACK_VERSION, BYSTACK_BIN_DIR,
             BYSTACK_RELEASE_KEYS_PEM (the keys a release is verified against)
EOF
}

uninstall() {
	if command -v systemctl >/dev/null 2>&1; then
		systemctl disable --now bystack-agent 2>/dev/null || true
		# The upgrade machinery, in the order it would otherwise fire: stop
		# watching for a trigger before removing the thing that answers it.
		for unit in bystack-agent-update.path bystack-agent-rollback.timer \
			bystack-agent-rollback.service; do
			systemctl disable --now "$unit" 2>/dev/null || true
		done
	fi
	rm -f "$UNIT" "${BIN_DIR}/bystack-agent" "${BIN_DIR}/bystack-agent.prev" \
		"${CONF_DIR}/agent.env" "${LIB_DIR}/agent-rollback.sh" \
		"${UNIT_DIR}/bystack-agent-update.path" \
		"${UNIT_DIR}/bystack-agent-updater.service" \
		"${UNIT_DIR}/bystack-agent-rollback.service" \
		"${UNIT_DIR}/bystack-agent-rollback.timer"
	rm -rf "$UPDATE_DIR"
	command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload
	# Left behind on purpose. The certificate in there is what the Controller
	# knows this host as; removing it turns "I reinstalled the agent" into "a
	# new host appeared and the old one went quiet", which is a worse thing to
	# have to work out at three in the morning. `rm -rf` it deliberately if
	# that is what you mean.
	echo "removed. ${STATE_DIR} kept -- it holds this host's certificate."
	exit 0
}

#: Split `RELEASE_KEYS_PEM` into one file per key, in the given directory.
#:
#: A *set*, tried in turn, which is what makes rotation two ordinary releases
#: rather than a flag day: the release signed by the new key is installed by an
#: installer that still accepts the old one, and the block for the old one is
#: deleted a release later.
#:
#: Echoes how many it wrote. The count is the caller's only way to tell "no
#: keys configured" from "a key block that did not parse", and those two want
#: different sentences.
split_keys() {
	keydir="$1"
	number=0
	current=""
	printf '%s\n' "$RELEASE_KEYS_PEM" | while IFS= read -r line; do
		case "$line" in
		*"BEGIN PUBLIC KEY"*)
			number=$((number + 1))
			current="${keydir}/key.${number}.pem"
			: >"$current"
			;;
		esac
		if [ -n "$current" ]; then
			printf '%s\n' "$line" >>"$current"
		fi
	done
	# Counted from the files rather than carried out of the loop: the pipe
	# above runs the body in a subshell, so `number` is back to 0 by here.
	# The files are the part that survives, and they are the part that matters.
	set -- "$keydir"/key.*.pem
	[ -f "$1" ] && echo $# || echo 0
}

#: `--keys`: what this installer will accept a release from, and nothing else.
#:
#: The trust chain begins with this file, so "which keys does the thing I am
#: about to run as root believe in" should be answerable without reading shell.
#: Printed as hex because that is the spelling `agent/keys/*.pub` uses and the
#: spelling an agent's own key set is written in -- the two are meant to match,
#: and a comparison nobody can perform by eye is one nobody performs.
print_keys() {
	if [ -z "$RELEASE_KEYS_PEM" ]; then
		echo "This installer trusts no release signing keys."
		echo
		echo "  Downloads are checked against SHA256SUMS, which proves the file was not"
		echo "  corrupted in transit and says nothing about who produced it. Set"
		echo "  BYSTACK_RELEASE_KEYS_PEM, or fetch an installer from a tag that has keys."
		exit 0
	fi
	command -v openssl >/dev/null 2>&1 ||
		die "no openssl, so the keys cannot be decoded (and a signature could not be checked either)."

	dir="$(mktemp -d)"
	# shellcheck disable=SC2064  # expanded now, not at trap time
	trap "rm -rf '$dir'" EXIT INT TERM
	count="$(split_keys "$dir")"
	[ "$count" -gt 0 ] || die "RELEASE_KEYS_PEM is set but holds no PEM public key block."

	echo "This installer accepts a release signed by any of these ${count} key(s):"
	echo
	for key in "$dir"/key.*.pem; do
		# An ed25519 SubjectPublicKeyInfo is a 12-byte header and the 32-byte
		# key, so the tail of the DER *is* the raw key. Cheaper and more
		# portable than asking openssl for a format it only learned recently.
		raw="$(openssl pkey -pubin -in "$key" -outform DER 2>/dev/null |
			tail -c 32 | od -An -tx1 | tr -d ' \n')"
		[ -n "$raw" ] || die "$key is not a public key openssl can read."
		echo "  ${raw}"
	done
	echo
	echo "The same values are in agent/keys/*.pub, compiled into the agent this"
	echo "installs, and are what every upgrade after this one is verified against."
	exit 0
}

while [ $# -gt 0 ]; do
	case "$1" in
	--controller) controller="${2:?--controller needs a URL}"; shift 2 ;;
	--token) token="${2:?--token needs a token}"; shift 2 ;;
	--binary) local_binary="${2:?--binary needs a path}"; shift 2 ;;
	--socket) docker_socket="${2:?--socket needs a path}"; shift 2 ;;
	--read-only) read_only=true; shift ;;
	--keys) print_keys ;;
	--uninstall) uninstall ;;
	-h | --help) usage; exit 0 ;;
	*) usage >&2; die "unknown argument $1" ;;
	esac
done

[ "$(id -u)" = 0 ] || die "must run as root (it installs a system service)"
[ -n "$controller" ] || { usage >&2; die "--controller is required"; }

case "$controller" in
wss://*) ;;
ws://*) die "refusing ws://: agent connections are mutually authenticated and there is no insecure mode (ADR-0011)" ;;
*) die "--controller must be a wss:// URL, got $controller" ;;
esac

command -v systemctl >/dev/null 2>&1 ||
	die "no systemd here. The agent is one static binary and needs no installer:
  copy it to ${BIN_DIR}/bystack-agent and run it under whatever supervises
  services on this machine. packaging/systemd/bystack-agent.service documents
  what it wants."

# --------------------------------------------------------------------------
# What this host was already installed with
# --------------------------------------------------------------------------
#
# An upgrade is this script run a second time, and everything below rewrites
# `agent.env` from the flags it was given -- so a host deliberately installed
# with `--read-only` would come back able to mutate, silently, because the
# operator pasted the upgrade line and that line has no reason to carry a
# decision made months ago on one machine out of forty.
#
# So an *absent* flag on a host that already has an agent means "leave it as it
# was". Passing one still wins, which is how an operator changes their mind,
# and the carried-over settings are printed rather than assumed: a mode this
# script chose for you and did not mention is the same problem in a quieter
# form.
existing="${CONF_DIR}/agent.env"
if [ -f "$existing" ]; then
	kept=""
	if [ -z "$docker_socket" ]; then
		docker_socket="$(sed -n 's/^BYSTACK_DOCKER_SOCKET=//p' "$existing" | tail -n 1)"
		[ -n "$docker_socket" ] && kept="${kept} --socket ${docker_socket}"
	fi
	if [ -z "$read_only" ]; then
		case "$(sed -n 's/^BYSTACK_READ_ONLY=//p' "$existing" | tail -n 1)" in
		true | TRUE | True | 1 | yes) read_only=true; kept="${kept} --read-only" ;;
		esac
	fi
	# An `if` and not `[ -n "$kept" ] && echo`, which is the same shape the
	# environment file below has to end with `true` to survive: a trailing
	# AND-list whose test is false is a failed command under `set -e`, and the
	# shells this runs on do not agree about whether that ends the script. An
	# installer that exits silently just before installing anything is not a
	# bug worth being clever for.
	if [ -n "$kept" ]; then
		echo "==> keeping this host's existing settings:${kept}"
	fi
fi

# --------------------------------------------------------------------------
# The binary
# --------------------------------------------------------------------------

arch="$(uname -m)"
case "$arch" in
x86_64 | amd64) arch=x86_64 ;;
aarch64 | arm64) arch=aarch64 ;;
*) die "no prebuilt agent for $arch. Build one with scripts/build-agent.sh --target ${arch}-unknown-linux-musl and pass --binary." ;;
esac

work="$(mktemp -d)"
# shellcheck disable=SC2064  # $work is wanted expanded now, not at trap time
trap "rm -rf '$work'" EXIT INT TERM
staged="${work}/bystack-agent"

#: The digest of a file, however this machine spells that command.
digest() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 "$1" | cut -d' ' -f1
	else
		die "no sha256sum or shasum; cannot verify the download"
	fi
}

#: Check a signed manifest against the keys above, and the binary against it.
#:
#: Returns non-zero when this installer holds no keys or has no openssl, which
#: leaves the caller on the checksum path. That is a real downgrade in what is
#: being proved and it is announced rather than silent: a checksum fetched over
#: the same connection as the binary it describes is an integrity check against
#: corruption, not against the origin.
verify_manifest() {
	manifest="$1"
	signature="$2"
	binary="$3"

	[ -n "$RELEASE_KEYS_PEM" ] || return 1
	command -v openssl >/dev/null 2>&1 || {
		echo "note: no openssl here, so the release signature cannot be checked."
		return 1
	}
	# Ed25519 verification from the command line needs `-rawin`, which is
	# OpenSSL 3.0. Probed rather than assumed, because the alternative is a
	# machine with 1.1.1 being told its release is not signed by a key we
	# trust -- a sentence that sends an operator to look for a compromise.
	openssl pkeyutl -help 2>&1 | grep -q -- '-rawin' || {
		echo "note: this openssl cannot verify ed25519 from the command line (needs 3.0)."
		return 1
	}

	keydir="${work}/keys"
	mkdir -p "$keydir"
	split_keys "$keydir" >/dev/null

	verified=false
	for key in "$keydir"/key.*.pem; do
		[ -f "$key" ] || continue
		if openssl pkeyutl -verify -pubin -inkey "$key" -rawin \
			-in "$manifest" -sigfile "$signature" >/dev/null 2>&1; then
			verified=true
			break
		fi
	done
	[ "$verified" = true ] ||
		die "the release manifest is not signed by any key this installer trusts.
  Either this is not our release, or the installer predates a key rotation --
  fetch the installer from the same tag as the binary."

	# Everything below reads a document whose signature has already been
	# checked, so none of it is defending against a forgery. It is defending
	# against a *genuine* signature over the wrong thing -- which is the only
	# attack left once the key holds, and the whole reason what gets signed is
	# a document rather than a digest.

	# The magic first. A manifest in a format this installer does not know is
	# one whose fields it would be reading by position and luck; refusing is
	# the behaviour `agent/src/upgrade.rs` has, and the two have to agree or a
	# format change breaks installs and upgrades differently.
	head -n 1 "$manifest" | grep -qx "bystack-manifest/1" ||
		die "this release's manifest is in a format this installer does not know.
  Fetch the installer from the same tag as the binary."

	# The name, because a key that ever signs a second artifact must not let
	# one be presented as the other. Nothing else signed by this key is meant
	# to end up in ${BIN_DIR}/bystack-agent.
	signed_name="$(sed -n 's/^name //p' "$manifest" | tail -n 1)"
	[ "$signed_name" = "bystack-agent" ] ||
		die "the signed manifest is for '${signed_name}', not bystack-agent"

	# The manifest is only worth what it says about *this* file.
	want="$(sed -n 's/^sha256 //p' "$manifest" | tail -n 1)"
	[ -n "$want" ] || die "the signed manifest names no sha256"
	got="$(digest "$binary")"
	[ "$want" = "$got" ] ||
		die "the binary does not match its signed manifest: expected $want, got $got"

	# `arch` and `version` are inside the signature for the reason the digest
	# alone is not enough: without them a genuinely signed *old* release can be
	# served as a new one, or one architecture's artifact as another's.
	signed_arch="$(sed -n 's/^arch //p' "$manifest" | tail -n 1)"
	[ "$signed_arch" = "$arch" ] ||
		die "this host is ${arch} and the signed release is for ${signed_arch}"
	echo "==> signature verified ($(sed -n 's/^version //p' "$manifest" | tail -n 1), ${signed_arch})"
	return 0
}

if [ -n "$local_binary" ]; then
	[ -f "$local_binary" ] || die "--binary $local_binary is not a file"
	cp "$local_binary" "$staged"
	# A file an operator put on the machine themselves. If the manifest is
	# beside it, it is checked; if it is not, the operator's own copy is the
	# authority and there is nothing here to second-guess it with.
	if [ -f "${local_binary}.manifest" ] && [ -f "${local_binary}.manifest.sig" ]; then
		verify_manifest "${local_binary}.manifest" "${local_binary}.manifest.sig" "$staged" || true
	fi
else
	base="https://github.com/${REPO}/releases/download/${VERSION}"
	echo "==> fetching bystack-agent ${VERSION} (${arch})"

	if command -v curl >/dev/null 2>&1; then
		fetch() { curl -fsSL "$1" -o "$2"; }
	elif command -v wget >/dev/null 2>&1; then
		fetch() { wget -qO "$2" "$1"; }
	else
		die "neither curl nor wget. Fetch ${base}/bystack-agent-${arch} yourself and pass --binary."
	fi

	fetch "${base}/bystack-agent-${arch}" "$staged" ||
		die "could not fetch ${base}/bystack-agent-${arch}"

	# The signature first, because it proves strictly more and a release that
	# has one has no reason to fall back. A release that does not -- or an
	# installer built before there was a key -- lands on the checksum below,
	# which is what every install did before ADR-0017.
	signed=false
	if [ -n "$RELEASE_KEYS_PEM" ] &&
		fetch "${base}/bystack-agent-${arch}.manifest" "${work}/manifest" &&
		fetch "${base}/bystack-agent-${arch}.manifest.sig" "${work}/manifest.sig"; then
		if verify_manifest "${work}/manifest" "${work}/manifest.sig" "$staged"; then
			signed=true
		fi
	fi

	if [ "$signed" = false ]; then
		fetch "${base}/SHA256SUMS" "${work}/SHA256SUMS" ||
			die "could not fetch ${base}/SHA256SUMS -- refusing to install an unverified binary"

		# The checksum is the whole reason a release publishes one. A download
		# over TLS proves who served it, not what they served, and this file is
		# about to be run as a service on every machine in a fleet.
		want="$(awk -v f="bystack-agent-${arch}" '$2 == f || $2 == "*"f {print $1}' "${work}/SHA256SUMS")"
		[ -n "$want" ] || die "SHA256SUMS names no bystack-agent-${arch}"
		got="$(digest "$staged")"
		[ "$want" = "$got" ] || die "checksum mismatch: expected $want, got $got"
	fi
fi

# Neither `cp` nor a download makes a file executable, so this has to happen
# before the binary can be asked anything.
chmod 0755 "$staged"

# Run it before installing it. `uname -m` is what the download was chosen by,
# and it is not the whole story: a 32-bit userland on a 64-bit kernel reports
# x86_64, and a truncated download reports nothing at all. A binary that
# answers `--version` is one this machine can actually execute.
"$staged" --version >/dev/null 2>&1 ||
	die "the fetched agent does not run on this machine (wrong architecture, or truncated)"

install -d -m 0755 "$BIN_DIR"
# `install` rather than `cp`: it replaces the inode instead of writing through
# it, so an agent that is running right now keeps the file it started with and
# the upgrade takes effect on the restart below rather than mid-syscall.
install -m 0755 "$staged" "${BIN_DIR}/bystack-agent"

# --------------------------------------------------------------------------
# The account, the configuration, the unit
# --------------------------------------------------------------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
	useradd --system --no-create-home --home-dir "$STATE_DIR" \
		--shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null ||
		adduser --system --no-create-home --home "$STATE_DIR" \
			--shell /usr/sbin/nologin "$SERVICE_USER" ||
		die "could not create the $SERVICE_USER account"
fi

# The group that owns the socket, which is not called `docker` everywhere and
# is not called anything at all on a host with no engine yet.
socket="${docker_socket:-/var/run/docker.sock}"
if [ -S "$socket" ]; then
	# `-L`, and it is load-bearing. GNU `stat` reports the *symlink* when it is
	# given one, and /var/run/docker.sock is a symlink on more hosts than not:
	# a podman-compat setup points it at /run/podman/podman.sock, and several
	# distributions link /var/run at /run. Without `-L` this reads the link's
	# own group -- `root`, mode 777, always -- writes `SupplementaryGroups=root`
	# into the unit, and the agent starts and cannot open the socket. The
	# symptom is "cannot reach the docker socket: Permission denied" on a host
	# whose socket is plainly readable by the group the operator checked.
	socket_group="$(stat -Lc '%G' "$socket" 2>/dev/null || echo docker)"
else
	socket_group=docker
	echo "note: no socket at ${socket} yet; the agent will say so and the unit will restart"
fi
#
# `SupplementaryGroups=` naming a group that does not exist is not a warning:
# systemd fails at step GROUP with status 216 before the binary is ever
# executed, and the journal line says "Failed to determine supplementary
# groups: No such process" -- which mentions neither Docker nor the group. On a
# host where the engine is not installed yet, or is rootless, or calls the
# group something else, that is a unit that will never start and an error that
# points nowhere. So the line is written only when there is a group to name.
supplementary=""
if getent group "$socket_group" >/dev/null 2>&1; then
	supplementary="SupplementaryGroups=${socket_group}"
	usermod -aG "$socket_group" "$SERVICE_USER" 2>/dev/null ||
		gpasswd -a "$SERVICE_USER" "$socket_group" >/dev/null 2>&1 || true
else
	echo "note: no '${socket_group}' group on this host, so the unit does not join one."
	echo "      Once the engine is installed:"
	echo "        usermod -aG \$(stat -Lc '%G' ${socket}) ${SERVICE_USER}"
	echo "        # then add SupplementaryGroups= to ${UNIT} and reload"
fi

install -d -m 0755 "$CONF_DIR"
umask 077
{
	echo "# Written by install-agent.sh. See packaging/systemd/agent.env.example."
	echo "BYSTACK_CONTROLLER=${controller}"
	[ -n "$token" ] && echo "BYSTACK_TOKEN=${token}"
	[ -n "$docker_socket" ] && echo "BYSTACK_DOCKER_SOCKET=${docker_socket}"
	[ -n "$read_only" ] && echo "BYSTACK_READ_ONLY=true"
	true
} >"${CONF_DIR}/agent.env"
chmod 0600 "${CONF_DIR}/agent.env"
umask 022

cat >"$UNIT" <<EOF
[Unit]
Description=ByStack agent
Documentation=https://github.com/${REPO}
After=network-online.target docker.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
Environment=BYSTACK_STATE_DIR=%S/bystack-agent
EnvironmentFile=${CONF_DIR}/agent.env
ExecStart=${BIN_DIR}/bystack-agent
Restart=always
RestartSec=10
KillSignal=SIGTERM

User=${SERVICE_USER}
Group=${SERVICE_USER}
${supplementary}
StateDirectory=bystack-agent
StateDirectoryMode=0700

NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectClock=yes
ProtectHostname=yes
ProtectKernelLogs=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
ProtectProc=invisible
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RemoveIPC=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
SystemCallArchitectures=native
MemoryMax=64M
TasksMax=32

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT"

# --------------------------------------------------------------------------
# The upgrade path (ADR-0017)
# --------------------------------------------------------------------------
#
# Four units and one script, and the arrangement is the whole point: **the
# only thing here with write access to ${BIN_DIR} has no network, and the
# thing with a network cannot write there.** The agent's unit above is
# unchanged by any of it -- no `ReadWritePaths`, nothing.
#
#   agent            verifies a signed release, stages it in its own
#                    StateDirectory, writes a trigger file
#   .path            sees the trigger file. No polkit, no arguments
#   updater.service  root, no network, installs and restarts the agent
#   rollback.timer   armed by the updater, stopped when probation resolves
#   rollback.service puts the previous binary back if the new one never
#                    reaches the Controller
#
# They are written here rather than fetched because this script is one file
# curl'd onto a machine, and an installer that needs five more downloads is an
# installer that fails halfway on the network it was meant to survive. The
# readable copies are `packaging/systemd/` and `packaging/agent-rollback.sh`,
# and they say the same thing at length.

# Where the agent stages what it has verified. Created here so the path unit
# has a directory to watch from the first boot, and owned by the agent because
# the agent is what writes into it.
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "${STATE_DIR}/upgrade" 2>/dev/null ||
	{
		mkdir -p "${STATE_DIR}/upgrade"
		chown "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}/upgrade" 2>/dev/null || true
		chmod 0700 "${STATE_DIR}/upgrade"
	}

cat >"${UNIT_DIR}/bystack-agent-update.path" <<EOF
[Unit]
Description=ByStack agent: watch for a staged upgrade
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0017-agent-upgrade-signed-push.md
After=bystack-agent.service

[Path]
PathExists=${STATE_DIR}/upgrade/trigger
Unit=bystack-agent-updater.service

[Install]
WantedBy=paths.target
EOF

cat >"${UNIT_DIR}/bystack-agent-updater.service" <<EOF
[Unit]
Description=ByStack agent: install a staged upgrade
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0017-agent-upgrade-signed-push.md
ConditionPathExists=${STATE_DIR}/upgrade/trigger
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=oneshot
Environment=BYSTACK_STATE_DIR=${STATE_DIR}
Environment=BYSTACK_AGENT_BIN=${BIN_DIR}/bystack-agent
ExecStart=${BIN_DIR}/bystack-agent apply-update

RuntimeDirectory=bystack-agent-updater
RuntimeDirectoryMode=0700
StateDirectory=bystack-agent-update

PrivateNetwork=yes
ProtectSystem=strict
ReadWritePaths=${BIN_DIR}
ReadWritePaths=${STATE_DIR}
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_CHOWN CAP_FOWNER CAP_FSETID
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
SystemCallArchitectures=native
MemoryMax=128M
TasksMax=32
EOF

cat >"${UNIT_DIR}/bystack-agent-rollback.service" <<EOF
[Unit]
Description=ByStack agent: undo an upgrade that never reached the Controller
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0017-agent-upgrade-signed-push.md

[Service]
Type=oneshot
Environment=BYSTACK_STATE_DIR=${STATE_DIR}
ExecStart=${LIB_DIR}/agent-rollback.sh
StateDirectory=bystack-agent-update

PrivateNetwork=yes
ProtectSystem=strict
ReadWritePaths=${BIN_DIR}
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER CAP_FSETID
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
SystemCallArchitectures=native
MemoryMax=64M
TasksMax=16

[Install]
WantedBy=multi-user.target
EOF

cat >"${UNIT_DIR}/bystack-agent-rollback.timer" <<EOF
[Unit]
Description=ByStack agent: check on an upgrade that is still on probation
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0017-agent-upgrade-signed-push.md

[Timer]
OnActiveSec=60
OnUnitActiveSec=60
AccuracySec=15s
Unit=bystack-agent-rollback.service
EOF

chmod 0644 "${UNIT_DIR}/bystack-agent-update.path" \
	"${UNIT_DIR}/bystack-agent-updater.service" \
	"${UNIT_DIR}/bystack-agent-rollback.service" \
	"${UNIT_DIR}/bystack-agent-rollback.timer"

install -d -m 0755 "$LIB_DIR"
# The rollback is a shell script and not a subcommand of the agent, for one
# reason: it has to work when the agent that was just installed does not. That
# is the entire case it exists for.
cat >"${LIB_DIR}/agent-rollback.sh" <<'ROLLBACK'
#!/bin/sh
# Undo an upgrade that never reached a Controller (ADR-0017).
# Written by install-agent.sh; the commented original is packaging/agent-rollback.sh.
set -eu

UPDATE_DIR=/var/lib/bystack-agent-update
PROBATION="${UPDATE_DIR}/probation"
STATE_DIR="${BYSTACK_STATE_DIR:-/var/lib/bystack-agent}"
CONNECTED="${STATE_DIR}/connected"
TIMER=bystack-agent-rollback.timer
SERVICE=bystack-agent.service

say() { echo "bystack-agent-rollback: $*"; }
field() { sed -n "s/^$1=//p" "$2" 2>/dev/null | tail -n 1; }
disarm() { systemctl stop "$TIMER" >/dev/null 2>&1 || true; }

if [ ! -f "$PROBATION" ]; then
	disarm
	exit 0
fi

version="$(field version "$PROBATION")"
installed_at="$(field installed_at "$PROBATION")"
deadline="$(field deadline "$PROBATION")"
binary="$(field binary "$PROBATION")"
previous="$(field previous "$PROBATION")"
now="$(date +%s)"

if [ -z "$version" ] || [ -z "$deadline" ] || [ -z "$binary" ]; then
	say "${PROBATION} is unreadable; clearing it and leaving the agent in place"
	rm -f "$PROBATION"
	disarm
	exit 0
fi

# The version has to match: a marker left by the *previous* binary would
# otherwise pass for evidence that this one works.
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
	systemctl start "$TIMER" >/dev/null 2>&1 || true
	exit 0
fi

say "${version} has been installed since ${installed_at} and has not reached a Controller"

if [ -n "$previous" ] && [ -f "$previous" ]; then
	# Created in the target directory and renamed within it: a file made
	# elsewhere keeps the SELinux type of where it was made, and the service
	# then fails to start in a way that looks like a bad binary.
	staged="$(dirname "$binary")/.bystack-agent.rollback.$$"
	cp -p "$previous" "$staged"
	chmod 0755 "$staged"
	mv -f "$staged" "$binary"
	if command -v restorecon >/dev/null 2>&1; then
		restorecon -F "$binary" || true
	fi
	printf '# Written by agent-rollback.sh.\nrolled_back_from=%s\nat=%s\nreason=%s\n' \
		"$version" "$now" "it did not reach a Controller within the probation window" \
		>"${UPDATE_DIR}/last-rollback"
	rm -f "$PROBATION"
	disarm
	say "restored ${previous} and restarting ${SERVICE}"
	systemctl restart "$SERVICE" || true
	exit 0
fi

say "no previous binary to restore. Leaving ${version} in place; this host needs a look."
printf '# Written by agent-rollback.sh.\nrolled_back_from=%s\nat=%s\nreason=%s\n' \
	"$version" "$now" "it did not reach a Controller, and there was no previous binary" \
	>"${UPDATE_DIR}/last-rollback"
rm -f "$PROBATION"
disarm
ROLLBACK
chmod 0755 "${LIB_DIR}/agent-rollback.sh"

systemctl daemon-reload
systemctl enable bystack-agent >/dev/null 2>&1 || true
# The path unit and the boot-time rollback check. The *timer* is deliberately
# not enabled: it is started by an install and stopped when the probation it
# was armed for resolves, so a host with nothing in flight wakes for nothing.
systemctl enable --now bystack-agent-update.path >/dev/null 2>&1 || true
systemctl enable bystack-agent-rollback.service >/dev/null 2>&1 || true

# The start limit above is deliberate, and it makes reinstalling the thing that
# breaks. An operator whose first attempt failed five times -- no token, wrong
# URL, engine not up yet -- fixes it and runs this again, and systemd refuses
# with "Start request repeated too quickly" about a unit that has just been
# rewritten. Clearing the counter is what makes the second attempt an attempt.
systemctl reset-failed bystack-agent >/dev/null 2>&1 || true

# A start that fails is the most likely thing to happen on a machine nobody
# has installed this on before, and "Job for bystack-agent.service failed"
# followed by instructions to go and read two other commands is the least
# useful possible response to it. The journal already contains the sentence
# that says what is wrong -- usually the Docker socket, usually a group.
if ! systemctl restart bystack-agent; then
	echo
	echo "the agent is installed but would not start:"
	journalctl -u bystack-agent -n 15 --no-pager 2>/dev/null | sed 's/^/  /'
	exit 1
fi

# --------------------------------------------------------------------------
# Did it work, and is the token still lying about
# --------------------------------------------------------------------------

# The agent enrols on its first connection, so the certificate appearing is the
# proof that the token was redeemed -- and the point after which the token in
# the environment file is spent. It stays there deliberately if enrolment did
# not happen, because a retry needs it and a token that is *gone* leaves an
# operator with no way forward except minting another.
#: The three files `Credentials::load` needs, named exactly as the agent
#: writes them (`agent/src/trust.rs`). Spelled out rather than globbed: this
#: loop deciding "not enrolled" is what leaves a spent token in `agent.env`,
#: and a token beside a certificate makes the agent re-enrol on its next
#: restart -- so a pattern that matches nothing does not merely print the
#: wrong sentence, it arms a failure that fires on the next upgrade or reboot.
#: `test_wire.py` checks these names against the Rust source.
CREDENTIALS="agent.crt agent.key ca.crt"

enrolled=false
i=0
while [ $i -lt 30 ]; do
	have=true
	for file in $CREDENTIALS; do
		[ -f "${STATE_DIR}/${file}" ] || have=false
	done
	# All three, not just the certificate. The agent writes them together, so
	# a directory holding some of them is one caught mid-enrolment, and
	# treating that as done would delete the token a retry still needs.
	if [ "$have" = true ]; then
		enrolled=true
		break
	fi
	sleep 1
	i=$((i + 1))
done

if [ "$enrolled" = true ]; then
	if [ -n "$token" ]; then
		# A spent token is not a secret, but it is a credential-shaped string in
		# a file that gets copied into backups and support bundles. And leaving
		# it has a second effect that is not cosmetic: the agent re-enrols
		# whenever a token sits beside a stored certificate, so every restart
		# from here would mint a new identity for a host that already has one.
		sed -i '/^BYSTACK_TOKEN=/d' "${CONF_DIR}/agent.env"
		systemctl restart bystack-agent
	fi
	echo
	echo "bystack-agent is running. This host has a certificate and is talking to"
	echo "${controller}."
	echo "If the Controller does not auto-approve, it is now waiting for you in Hosts."
else
	echo
	echo "bystack-agent is installed and running, but has not enrolled yet."
	echo "The token is still in ${CONF_DIR}/agent.env so it can keep trying."
	echo
	echo "  systemctl status bystack-agent"
	echo "  journalctl -u bystack-agent -n 50"
	exit 1
fi
