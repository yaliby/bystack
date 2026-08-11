#!/bin/sh
#
# Put the ByStack agent on this machine.
#
#     curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.2.0/scripts/install-agent.sh \
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
#     curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.2.0/scripts/install-agent.sh \
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
VERSION="${BYSTACK_VERSION:-v0.2.0}"

BIN_DIR="${BYSTACK_BIN_DIR:-/usr/local/bin}"
CONF_DIR=/etc/bystack
UNIT=/etc/systemd/system/bystack-agent.service
SERVICE_USER=bystack-agent
STATE_DIR=/var/lib/bystack-agent

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
  --uninstall        Stop the service and remove everything except the
                     certificate, which is this host's identity

Environment: BYSTACK_REPO, BYSTACK_VERSION, BYSTACK_BIN_DIR
EOF
}

uninstall() {
	if command -v systemctl >/dev/null 2>&1; then
		systemctl disable --now bystack-agent 2>/dev/null || true
	fi
	rm -f "$UNIT" "${BIN_DIR}/bystack-agent" "${CONF_DIR}/agent.env"
	command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload
	# Left behind on purpose. The certificate in there is what the Controller
	# knows this host as; removing it turns "I reinstalled the agent" into "a
	# new host appeared and the old one went quiet", which is a worse thing to
	# have to work out at three in the morning. `rm -rf` it deliberately if
	# that is what you mean.
	echo "removed. ${STATE_DIR} kept -- it holds this host's certificate."
	exit 0
}

while [ $# -gt 0 ]; do
	case "$1" in
	--controller) controller="${2:?--controller needs a URL}"; shift 2 ;;
	--token) token="${2:?--token needs a token}"; shift 2 ;;
	--binary) local_binary="${2:?--binary needs a path}"; shift 2 ;;
	--socket) docker_socket="${2:?--socket needs a path}"; shift 2 ;;
	--read-only) read_only=true; shift ;;
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

staged="$(mktemp)"
trap 'rm -f "$staged"' EXIT INT TERM

if [ -n "$local_binary" ]; then
	[ -f "$local_binary" ] || die "--binary $local_binary is not a file"
	cp "$local_binary" "$staged"
else
	base="https://github.com/${REPO}/releases/download/${VERSION}"
	echo "==> fetching bystack-agent ${VERSION} (${arch})"

	sums="$(mktemp)"
	# shellcheck disable=SC2064  # $sums is wanted expanded now, not at trap time
	trap "rm -f '$staged' '$sums'" EXIT INT TERM

	if command -v curl >/dev/null 2>&1; then
		fetch() { curl -fsSL "$1" -o "$2"; }
	elif command -v wget >/dev/null 2>&1; then
		fetch() { wget -qO "$2" "$1"; }
	else
		die "neither curl nor wget. Fetch ${base}/bystack-agent-${arch} yourself and pass --binary."
	fi

	fetch "${base}/bystack-agent-${arch}" "$staged" ||
		die "could not fetch ${base}/bystack-agent-${arch}"
	fetch "${base}/SHA256SUMS" "$sums" ||
		die "could not fetch ${base}/SHA256SUMS -- refusing to install an unverified binary"

	# The checksum is the whole reason a release publishes one. A download over
	# TLS proves who served it, not what they served, and this file is about to
	# be run as a service on every machine in a fleet.
	want="$(awk -v f="bystack-agent-${arch}" '$2 == f || $2 == "*"f {print $1}' "$sums")"
	[ -n "$want" ] || die "SHA256SUMS names no bystack-agent-${arch}"
	if command -v sha256sum >/dev/null 2>&1; then
		got="$(sha256sum "$staged" | cut -d' ' -f1)"
	elif command -v shasum >/dev/null 2>&1; then
		got="$(shasum -a 256 "$staged" | cut -d' ' -f1)"
	else
		die "no sha256sum or shasum; cannot verify the download"
	fi
	[ "$want" = "$got" ] || die "checksum mismatch: expected $want, got $got"
fi

# `mktemp` makes a 0600 file and neither `cp` nor a download restores the mode,
# so this has to happen before the binary can be asked anything.
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
	socket_group="$(stat -c '%G' "$socket" 2>/dev/null || echo docker)"
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
	echo "        usermod -aG \$(stat -c '%G' ${socket}) ${SERVICE_USER}"
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

systemctl daemon-reload
systemctl enable bystack-agent >/dev/null 2>&1 || true

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
enrolled=false
i=0
while [ $i -lt 30 ]; do
	if [ -n "$(find "$STATE_DIR" -name '*.pem' -print -quit 2>/dev/null)" ]; then
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
