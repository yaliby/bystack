#!/bin/sh
#
# Put the ByStack Controller on this machine, as one file that can replace
# itself (ADR-0018).
#
#     curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.5.0/scripts/install-controller.sh \
#       | sudo sh
#
# That is the whole install. It fetches two signed binaries, creates the
# account and the directory they live in, writes a configuration file and four
# units, and starts the Controller. When it finishes there is a dashboard on
# http://127.0.0.1:8000 and an **Update system** button in it that works --
# which is the reason this script exists rather than a paragraph of `install`
# commands. The button is the only upgrade path that is atomic and undoable,
# and it is only available on an install shaped like this one.
#
# ## Why a second installer, when there is `docker compose up`
#
# The container is the easier install and stays the recommended one for a
# machine that already runs containers. What it cannot do is update itself:
# there is no single file inside an image for anything to rename, so `GET
# /controller` answers `updatable: false` and the dashboard says "upgrade the
# image". Which is honest, and is still an ssh.
#
# This install path is the one ADR-0018 was written for. The Controller is a
# zipapp -- one file carrying the Controller, its dependencies, the dashboard
# and the agent binary -- so replacing it is a `rename` with the previous inode
# kept beside it, and *undoing* that is another `rename`. Everything the
# **Update system** button promises rests on that one property.
#
# ## What gets installed
#
#     /opt/bystack/bin/bystack-controller   the zipapp. Replaced by rename
#     /opt/bystack/bin/bystack-manager      root's updater. Pulls and verifies
#     /opt/bystack/ipc/                     root:bystack 1770, the two files
#                                           the Controller and root talk through
#     /opt/bystack/releases/                signed agents, for the fleet
#     /opt/bystack/state/                   root only: the rollback record
#     /etc/bystack/bystack.yaml             configuration, written once
#     /var/lib/bystack/                     the CA key and the enrolment
#                                           registry. The one directory worth
#                                           backing up, and never deleted here
#
# Four units: the Controller, the path unit that watches for an update request,
# the oneshot that performs one, and the boot-time undo for an update that was
# interrupted. `packaging/systemd/` holds the same four with the reasoning
# written on them at length; they are written out here rather than fetched
# because this script is one file curl'd onto a machine, and an installer that
# needs five more downloads is one that fails half-way on the network it was
# meant to survive.
#
# ## The interpreter is pinned, and this checks for it first
#
# A zipapp carries wheels, and wheels with compiled extensions are built for
# one CPython ABI -- so a release is assembled for one minor version. This host
# needs a `python3.12` on its PATH. That is checked here, before anything is
# downloaded, because the alternative is finding out from a service that will
# not start.
#
# ## Offline, on a machine that cannot reach GitHub
#
#     scripts/build-agent.sh --package bystack-manager        # on a machine that can
#     scripts/build-controller.sh --arch x86_64
#     scp dist/bystack-controller-x86_64 dist/bystack-manager-x86_64 host:/tmp/
#     sudo sh install-controller.sh --binary /tmp/bystack-controller-x86_64 \
#       --manager /tmp/bystack-manager-x86_64
#
# ## After this, upgrades are a button
#
# `bystack-manager` pulls the next release from GitHub, checks it against a key
# compiled into itself, runs it once to see that it starts *before stopping
# anything*, swaps it, and puts the previous file back if the new Controller
# does not answer within three minutes. Re-running this script also upgrades,
# and is the path for a host with no egress -- but it is a plain replace with
# no probation and no rollback, so it is the second choice on a machine that
# can reach the internet.
#
# ## What it deliberately does not do
#
# It does not install Python, does not install or configure a container engine,
# and does not open the fleet listener unless asked (`--agents`). The first two
# are decisions about a machine that belong to whoever owns it. The third is a
# second port on a real network, and an installer that opened one because it
# could is an installer that decided something nobody asked it.
#
# POSIX sh, not bash: this runs on whatever a stranger's server has.
#
set -eu

REPO="${BYSTACK_REPO:-yaliby/bystack}"
VERSION="${BYSTACK_VERSION:-v0.5.0}"

#: One folder, and everything in it (ADR-0018). Overridable for a second
#: Controller on one machine, which is a real thing to want while testing an
#: upgrade and not a thing to have to be root in `/opt` to try.
HOME_DIR="${BYSTACK_HOME:-/opt/bystack}"
CONF_DIR=/etc/bystack
CONF="${CONF_DIR}/bystack.yaml"
UNIT_DIR=/etc/systemd/system
SERVICE_USER=bystack
STATE_DIR=/var/lib/bystack

#: The CPython the release is assembled for. Not a preference: `shiv` builds
#: the zipapp from wheels tagged for one ABI (`scripts/build-controller.sh`),
#: and the artifact's own shebang names this. A host without it can install
#: nothing here, and finding that out now costs a sentence.
PYTHON_VERSION=3.12

#: Public keys that releases are signed with, PEM, concatenated.
#:
#: The same keys as `scripts/install-agent.sh` and as `agent/keys/release.pub`,
#: in the encoding openssl wants. `scripts/check-release-keys.sh` fails the
#: build if the three disagree, because a mismatch is invisible until it is a
#: Controller that installed fine and then refused every update after it.
#:
#: Both binaries this installs are checked against these. That matters most for
#: `bystack-manager`, which is where the key set for every *later* update is
#: compiled in: it is the root of the chain, and the one file here that nothing
#: downstream re-checks.
#:
#: More than one is legal and is how a key is rotated: concatenate the PEM
#: blocks, ship a release signed by the new one, then drop the old block.
#: `scripts/sign-agent.py keygen` prints what goes here.
RELEASE_KEYS_PEM="-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAFyeyEKNSP2/z8wQKqd9Fq99J/8NwJvNPyAdD7rTPDvM=
-----END PUBLIC KEY-----"

if [ -n "${BYSTACK_RELEASE_KEYS_PEM:-}" ]; then
	RELEASE_KEYS_PEM="$BYSTACK_RELEASE_KEYS_PEM"
fi

local_controller=""
local_manager=""
bind_host=127.0.0.1
bind_port=8000
agents_enabled=false
server_names=""
docker_socket=/var/run/docker.sock
start=true

die() {
	echo "install-controller.sh: $*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
usage: install-controller.sh [options]

  --agents           Turn on the fleet listener, so other servers can dial in.
                     Needs at least one --server-name.
  --server-name NAME An address your other servers will use to reach this one,
                     exactly as they will type it. Repeatable.
  --bind ADDR        What the dashboard listens on (default 127.0.0.1). Use
                     0.0.0.0 only on a private network, or behind your own
                     login page -- nothing here asks a browser for a password.
  --port N           Dashboard port (default 8000)
  --socket PATH      Container engine socket (default /var/run/docker.sock)
  --binary PATH      Install this bystack-controller instead of downloading one
  --manager PATH     Install this bystack-manager instead of downloading one
  --no-start         Install everything and leave the service stopped
  --keys             Print the release signing keys this installer trusts,
                     and exit. Worth doing before piping this into root.
  --uninstall        Stop everything and delete /opt/bystack. Keeps
                     /var/lib/bystack -- the CA key and the enrolment registry.

Environment: BYSTACK_REPO, BYSTACK_VERSION, BYSTACK_HOME,
             BYSTACK_RELEASE_BASE (a mirror to fetch the release from),
             BYSTACK_RELEASE_KEYS_PEM (the keys a release is verified against)
EOF
}

units() {
	echo "bystack-controller.service bystack-manager.path bystack-manager.service \
bystack-manager-rollback.service"
}

uninstall() {
	if command -v systemctl >/dev/null 2>&1; then
		# The watcher first: disabling the path unit before the thing it starts
		# means an intent file left lying around cannot start an update against
		# a half-removed install.
		for unit in bystack-manager.path bystack-manager-rollback.service \
			bystack-manager.service bystack-controller.service; do
			systemctl disable --now "$unit" 2>/dev/null || true
		done
	fi
	for unit in $(units); do
		rm -f "${UNIT_DIR}/${unit}"
	done
	command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload
	# One directory, deleted whole. That is the promise ADR-0018 makes about
	# this install shape and it is the reason everything lives under it.
	rm -rf "$HOME_DIR"
	echo "removed ${HOME_DIR} and the units."
	echo
	# Kept for the reason install-agent.sh keeps a host's certificate: this
	# directory *is* the fleet's identity. The CA private key in it is what
	# every enrolled agent's certificate chains to, so deleting it does not
	# uninstall a Controller -- it re-enrols a fleet.
	echo "${STATE_DIR} kept: it holds the CA key, the enrolment registry and the"
	echo "audit log. Losing it means re-approving every host. Delete it by hand if"
	echo "that is what you mean."
	echo "${CONF} kept too, so a reinstall comes back with the same settings."
	exit 0
}

#: Split `RELEASE_KEYS_PEM` into one file per key, in the given directory.
#:
#: A *set*, tried in turn, which is what makes rotation two ordinary releases
#: rather than a flag day. Echoes how many it wrote, because "no keys
#: configured" and "a key block that did not parse" want different sentences
#: and the caller cannot otherwise tell them apart.
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
	set -- "$keydir"/key.*.pem
	[ -f "$1" ] && echo $# || echo 0
}

#: `--keys`: what this installer will accept a release from, and nothing else.
#:
#: The trust chain begins here, so "which keys does the thing I am about to run
#: as root believe in" should be answerable without reading shell. Printed as
#: hex because that is the spelling `agent/keys/*.pub` uses -- the two are meant
#: to match, and a comparison nobody can perform by eye is one nobody performs.
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
		raw="$(openssl pkey -pubin -in "$key" -outform DER 2>/dev/null |
			tail -c 32 | od -An -tx1 | tr -d ' \n')"
		[ -n "$raw" ] || die "$key is not a public key openssl can read."
		echo "  ${raw}"
	done
	echo
	echo "The same values are in agent/keys/*.pub, compiled into the bystack-manager"
	echo "this installs, and are what every update after this one is verified against."
	exit 0
}

while [ $# -gt 0 ]; do
	case "$1" in
	--agents) agents_enabled=true; shift ;;
	--server-name) server_names="${server_names} ${2:?--server-name needs a name}"; shift 2 ;;
	--bind) bind_host="${2:?--bind needs an address}"; shift 2 ;;
	--port) bind_port="${2:?--port needs a number}"; shift 2 ;;
	--socket) docker_socket="${2:?--socket needs a path}"; shift 2 ;;
	--binary) local_controller="${2:?--binary needs a path}"; shift 2 ;;
	--manager) local_manager="${2:?--manager needs a path}"; shift 2 ;;
	--no-start) start=false; shift ;;
	--keys) print_keys ;;
	--uninstall) uninstall ;;
	-h | --help) usage; exit 0 ;;
	*) usage >&2; die "unknown argument $1" ;;
	esac
done

[ "$(id -u)" = 0 ] || die "must run as root (it installs a system service)"

command -v systemctl >/dev/null 2>&1 ||
	die "no systemd here, and every safety property of this install is a unit:
  the path unit that turns a button into a root action, the oneshot that
  performs an update, and the boot-time rollback for one that was interrupted.
  Run the container instead -- see INSTALL.md -- which needs no systemd and
  is upgraded by pulling a new image."

case "$bind_port" in
'' | *[!0-9]*) die "--port takes a number, got '${bind_port}'" ;;
esac

if [ "$agents_enabled" = true ] && [ -z "$server_names" ]; then
	die "--agents needs at least one --server-name.
  The fleet listener issues itself a certificate for the names in that list,
  and an agent that dialled a name not in it fails the handshake -- so a
  listener turned on with no names is a port that refuses every agent. Pass
  the address your other servers will use, e.g. --server-name 10.0.0.5"
fi

# --------------------------------------------------------------------------
# The interpreter, before anything is downloaded
# --------------------------------------------------------------------------
#
# The failure this prevents is the one ADR-0018 spends a paragraph on. A host
# with no python3.12 passes every other check there is -- the signature holds,
# the digest matches, the architecture is right -- and then discovers it by
# having a service that will not start. The manager does this check before it
# stops anything, for the same reason, and this is the same check one step
# earlier: before anything is fetched at all.
python_bin="python${PYTHON_VERSION}"
command -v "$python_bin" >/dev/null 2>&1 || die "no ${python_bin} on this host.

  The Controller is shipped as one file for a reason -- it is what makes an
  update a rename, and a rollback another one -- and a single file carrying
  compiled Python wheels is built for exactly one CPython version.

  Install it and run this again:

    apt install python${PYTHON_VERSION}        # Debian, Ubuntu
    dnf install python${PYTHON_VERSION}        # Fedora, RHEL

  Or run the container instead, which carries its own interpreter and is
  upgraded by pulling a new image. INSTALL.md has both."

# --------------------------------------------------------------------------
# The binaries
# --------------------------------------------------------------------------

arch="$(uname -m)"
case "$arch" in
x86_64 | amd64) arch=x86_64 ;;
aarch64 | arm64) arch=aarch64 ;;
*) die "no prebuilt Controller for ${arch}. scripts/build-controller.sh --arch ${arch}
  builds one on a machine that can, and --binary installs it." ;;
esac

work="$(mktemp -d)"
# shellcheck disable=SC2064  # $work is wanted expanded now, not at trap time
trap "rm -rf '$work'" EXIT INT TERM

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
#: Returns non-zero when this installer holds no keys or has no usable openssl,
#: which leaves the caller on the checksum path. That is a real downgrade in
#: what is being proved and it is announced rather than silent: a checksum
#: fetched over the same connection as the binary it describes is an integrity
#: check against corruption, not against the origin.
verify_manifest() {
	manifest="$1"
	signature="$2"
	binary="$3"
	expect_name="$4"

	[ -n "$RELEASE_KEYS_PEM" ] || return 1
	command -v openssl >/dev/null 2>&1 || {
		echo "note: no openssl here, so the release signature cannot be checked."
		return 1
	}
	# Ed25519 from the command line needs `-rawin`, which is OpenSSL 3.0.
	# Probed rather than assumed: telling a machine with 1.1.1 that its release
	# is not signed by a key we trust is a sentence that sends an operator
	# looking for a compromise.
	openssl pkeyutl -help 2>&1 | grep -q -- '-rawin' || {
		echo "note: this openssl cannot verify ed25519 from the command line (needs 3.0)."
		return 1
	}

	keydir="${work}/keys"
	rm -rf "$keydir"
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
		die "${expect_name}'s manifest is not signed by any key this installer trusts.
  Either this is not our release, or the installer predates a key rotation --
  fetch the installer from the same tag as the binaries."

	# Everything below reads a document whose signature has already been
	# checked, so none of it is defending against a forgery. It defends against
	# a *genuine* signature over the wrong thing, which is the only attack left
	# once the key holds -- and the whole reason what gets signed is a document
	# rather than a digest.
	head -n 1 "$manifest" | grep -qx "bystack-manifest/1" ||
		die "${expect_name}'s manifest is in a format this installer does not know.
  Fetch the installer from the same tag as the binaries."

	# The name, because one key signs three artifacts now and a key that signs
	# more than one must not let one be presented as another. A genuine,
	# current, correctly signed Controller installed as the *manager* would
	# pass every other check on this page.
	signed_name="$(sed -n 's/^name //p' "$manifest" | tail -n 1)"
	[ "$signed_name" = "$expect_name" ] ||
		die "the signed manifest is for '${signed_name}', not ${expect_name}"

	want="$(sed -n 's/^sha256 //p' "$manifest" | tail -n 1)"
	[ -n "$want" ] || die "${expect_name}'s signed manifest names no sha256"
	got="$(digest "$binary")"
	[ "$want" = "$got" ] ||
		die "${expect_name} does not match its signed manifest: expected $want, got $got"

	signed_arch="$(sed -n 's/^arch //p' "$manifest" | tail -n 1)"
	[ "$signed_arch" = "$arch" ] ||
		die "this host is ${arch} and the signed ${expect_name} is for ${signed_arch}"
	echo "==> ${expect_name}: signature verified ($(sed -n 's/^version //p' "$manifest" |
		tail -n 1), ${signed_arch})"
	return 0
}

if command -v curl >/dev/null 2>&1; then
	fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
	fetch() { wget -qO "$2" "$1"; }
else
	fetch() { die "neither curl nor wget on this host; use --binary and --manager"; }
fi

#: Where the release comes from, and the one place an operator may move it.
#:
#: `bystack-manager` takes the same variable for the same reason (ADR-0018): an
#: air-gapped install is an ordinary deployment, and the signature is what makes
#: *where the bytes came from* an operational question rather than a trust one.
#: Having the updater honour a mirror and the installer refuse one would mean a
#: machine that could keep itself current and could not be set up in the first
#: place.
#:
#: Plaintext is allowed **only** here, and only because an operator typed it.
#: The default is composed from a repository written in this file and can never
#: be anything but https.
if [ -n "${BYSTACK_RELEASE_BASE:-}" ]; then
	base="${BYSTACK_RELEASE_BASE%/}/${VERSION}"
	echo "==> using the mirror at ${base}"
	echo "    Signatures are still checked against the keys above; a mirror moves"
	echo "    the bytes, not the trust."
else
	base="https://github.com/${REPO}/releases/download/${VERSION}"
fi
sums_fetched=false

#: Fetch one artifact and prove it is what the release says it is.
#:
#: The signature first, because it proves strictly more and a release that has
#: one has no reason to fall back. A release signed after publication -- which
#: is the ordinary window, since the key is offline (`release-agent.sh`) -- or
#: an installer built before there was a key lands on the checksum instead, and
#: says so.
obtain() {
	name="$1"
	staged="$2"
	from="$3"

	if [ -n "$from" ]; then
		[ -f "$from" ] || die "$from is not a file"
		cp "$from" "$staged"
		# A file an operator put on the machine themselves. If the manifest is
		# beside it, it is checked; if it is not, their own copy is the
		# authority and there is nothing here to second-guess it with.
		if [ -f "${from}.manifest" ] && [ -f "${from}.manifest.sig" ]; then
			verify_manifest "${from}.manifest" "${from}.manifest.sig" "$staged" "$name" || true
		else
			echo "==> ${name}: installed from ${from}, unverified (no manifest beside it)"
		fi
		return 0
	fi

	echo "==> fetching ${name}-${arch} ${VERSION}"
	fetch "${base}/${name}-${arch}" "$staged" ||
		die "could not fetch ${base}/${name}-${arch}.
  If this tag has no release yet, build the artifacts on a machine that can and
  pass --binary and --manager."

	if [ -n "$RELEASE_KEYS_PEM" ] &&
		fetch "${base}/${name}-${arch}.manifest" "${work}/${name}.manifest" &&
		fetch "${base}/${name}-${arch}.manifest.sig" "${work}/${name}.manifest.sig"; then
		if verify_manifest "${work}/${name}.manifest" "${work}/${name}.manifest.sig" \
			"$staged" "$name"; then
			return 0
		fi
	fi

	if [ "$sums_fetched" = false ]; then
		fetch "${base}/SHA256SUMS" "${work}/SHA256SUMS" ||
			die "could not fetch ${base}/SHA256SUMS -- refusing to install an unverified binary"
		sums_fetched=true
	fi
	want="$(awk -v f="${name}-${arch}" '$2 == f || $2 == "*"f {print $1}' "${work}/SHA256SUMS")"
	[ -n "$want" ] || die "SHA256SUMS names no ${name}-${arch}"
	got="$(digest "$staged")"
	[ "$want" = "$got" ] || die "checksum mismatch on ${name}-${arch}: expected $want, got $got"
	echo "==> ${name}: checksum matches SHA256SUMS (unsigned release, or no key here)"
}

controller_staged="${work}/bystack-controller"
manager_staged="${work}/bystack-manager"
obtain bystack-controller "$controller_staged" "$local_controller"
obtain bystack-manager "$manager_staged" "$local_manager"
chmod 0755 "$controller_staged" "$manager_staged"

# Run them before installing them. `uname -m` chose the download and is not the
# whole story: a 32-bit userland on a 64-bit kernel reports x86_64, a truncated
# download reports nothing, and the zipapp needs an interpreter that the check
# further up proved is *present* rather than proved *works*.
#
# SHIV_ROOT into the work directory: a zipapp unpacks itself on first run, and
# doing that as root into the account's cache would leave files the Controller
# then cannot write.
SHIV_ROOT="${work}/shiv" "$controller_staged" --version >/dev/null 2>&1 ||
	die "the fetched Controller does not run on this machine.
  It is a zipapp whose first line names ${python_bin}; check that
  \`${python_bin} -V\` works for root, and that the download is not truncated."
"$manager_staged" --version >/dev/null 2>&1 ||
	die "the fetched bystack-manager does not run on this machine (wrong architecture,
  or truncated)"
installing="$(SHIV_ROOT="${work}/shiv" "$controller_staged" --version | awk '{print $NF}')"

# --------------------------------------------------------------------------
# The account
# --------------------------------------------------------------------------
#
# Created before the directories, because two of them are owned by it and one
# of those modes is load-bearing: `ipc/` is root:bystack 1770, and the sticky
# bit is what stops the Controller unlinking root's report of how an update
# went (ADR-0018).
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
	useradd --system --no-create-home --home-dir "$STATE_DIR" \
		--shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null ||
		adduser --system --no-create-home --home "$STATE_DIR" \
			--shell /usr/sbin/nologin "$SERVICE_USER" ||
		die "could not create the ${SERVICE_USER} account"
fi
# `useradd --system` creates a matching group on most distributions and not on
# all of them, and the unit names both.
if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
	groupadd --system "$SERVICE_USER" 2>/dev/null ||
		addgroup --system "$SERVICE_USER" ||
		die "could not create the ${SERVICE_USER} group"
	usermod -g "$SERVICE_USER" "$SERVICE_USER" 2>/dev/null || true
fi

# The group that owns the engine socket, which is not called `docker`
# everywhere and is not called anything at all on a host with no engine yet.
#
# `SupplementaryGroups=` naming a group that does not exist is not a warning:
# systemd fails at step GROUP with status 216 before the binary is executed,
# and the journal line mentions neither Docker nor the group. So the line is
# written only when there is a group to name.
if [ -S "$docker_socket" ]; then
	# `-L`, and it is load-bearing. GNU `stat` reports the *symlink* when it is
	# given one, and /var/run/docker.sock is a symlink on more hosts than not:
	# a podman-compat setup points it at /run/podman/podman.sock, and several
	# distributions link /var/run at /run. Without `-L` this reads the link's
	# own group -- `root`, mode 777, always -- writes `SupplementaryGroups=root`
	# into the unit, and the agent starts and cannot open the socket. The
	# symptom is "cannot reach the docker socket: Permission denied" on a host
	# whose socket is plainly readable by the group the operator checked.
	socket_group="$(stat -Lc '%G' "$docker_socket" 2>/dev/null || echo docker)"
else
	socket_group=docker
	echo "note: no socket at ${docker_socket} yet. The Controller will start and the"
	echo "      map will simply not include this machine's own containers."
fi
supplementary=""
if getent group "$socket_group" >/dev/null 2>&1; then
	supplementary="SupplementaryGroups=${socket_group}"
	usermod -aG "$socket_group" "$SERVICE_USER" 2>/dev/null ||
		gpasswd -a "$SERVICE_USER" "$socket_group" >/dev/null 2>&1 || true
else
	echo "note: no '${socket_group}' group on this host, so the unit does not join one."
	echo "      Once the engine is installed:"
	echo "        usermod -aG \$(stat -Lc '%G' ${docker_socket}) ${SERVICE_USER}"
	echo "        # then add SupplementaryGroups= to the unit and reload"
fi

# --------------------------------------------------------------------------
# The directory
# --------------------------------------------------------------------------
#
# Every mode here is a decision, and the two that matter are the two the
# Controller can write to at all.
install -d -m 0755 "${HOME_DIR}" "${HOME_DIR}/bin"
# root:bystack 1770. The Controller creates `update.intent`; the sticky bit
# stops it unlinking `update.status`, which root owns and which is the one
# thing in this feature the Controller cannot say for itself.
install -d -m 1770 -o root -g "$SERVICE_USER" "${HOME_DIR}/ipc"
# Root's own. The Controller is the process the rollback record exists to
# undo, so a directory it could write to is a rollback it could cancel.
install -d -m 0700 -o root -g root "${HOME_DIR}/state"
# Where the manager drops the fleet's signed agents and the Controller reads
# them. It sends those bytes on; it never executes them.
install -d -m 0755 -o root -g root "${HOME_DIR}/releases"
# Where the zipapp unpacks itself, once per version. Owned by the account that
# runs it, which is why `SHIV_ROOT` names it in the unit.
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "${HOME_DIR}/cache"

# `install` rather than `cp`: it replaces the inode instead of writing through
# it, so a Controller that is running right now keeps the file it started with
# and the change takes effect at the restart below rather than mid-import.
install -m 0755 "$controller_staged" "${HOME_DIR}/bin/bystack-controller"
install -m 0755 "$manager_staged" "${HOME_DIR}/bin/bystack-manager"
command -v restorecon >/dev/null 2>&1 &&
	restorecon -F "${HOME_DIR}/bin/bystack-controller" "${HOME_DIR}/bin/bystack-manager" \
		>/dev/null 2>&1 || true

# --------------------------------------------------------------------------
# The configuration
# --------------------------------------------------------------------------
#
# Written once and never rewritten. An operator who turned on the fleet
# listener, added server names or changed the bind address did so in this file,
# and an installer that overwrote it on the next run would silently undo a
# decision that is not repeated on the command line -- the same trap
# `install-agent.sh` avoids by carrying flags over.
#
# Two settings in it are not preferences and are checked below if the file
# already exists:
#
#   agents.releases_dir     where `bystack-manager` puts the fleet's signed
#                           agents after it updates this Controller. Point it
#                           somewhere else and phase two of an update lands in
#                           a directory nothing reads.
#   agents.manager_ipc_dir  the two files that make the button work at all.
install -d -m 0755 "$CONF_DIR"
if [ -f "$CONF" ]; then
	echo "==> ${CONF} kept (it already exists)"
	for setting in "releases_dir: ${HOME_DIR}/releases" \
		"manager_ipc_dir: ${HOME_DIR}/ipc"; do
		key="${setting%%:*}"
		grep -q "^[[:space:]]*${key}:" "$CONF" || {
			echo "note: ${CONF} does not set ${key}. Add this under \`agents:\`, or the"
			echo "      update button will work and its second half will not:"
			echo "        ${setting}"
		}
	done
else
	# **The operator's names come first, and that ordering is load-bearing.**
	#
	# The Controller hands out one dial address, and it is `server_names[0]`
	# (`AgentsConfig.dial_url`): a wildcard bind is a meaningless thing to
	# dial, so the first name it issued itself a certificate for is what every
	# `--controller wss://...` gets. Put the loopback names first and every
	# agent on every *other* machine is told to dial `localhost`, which is
	# itself -- an install that looks perfect and enrols nothing, with
	# "Connection refused" on a machine where nothing is wrong.
	#
	# The loopback names stay in the list, after: they are what a local agent
	# and a browser on this machine use, and they cost nothing behind a name
	# that is actually routable.
	names=""
	for name in $server_names localhost 127.0.0.1 ::1; do
		names="${names}\"${name}\", "
	done
	cat >"$CONF" <<EOF
# ByStack, written by install-controller.sh. Yours to edit.
#
# Everything not named here is a default, and every setting there is has its
# reasoning written on it in backend/bystack.example.yaml -- which is the file
# to read to understand any of this.
#
# After an edit:  systemctl restart bystack-controller

agents:
  # Whether other servers can dial in. Off until you have a second machine:
  # it is a port on a real network, and turning one on is a decision rather
  # than a default. Set it to true, put the address your other servers will
  # use in server_names below, and restart.
  enabled: ${agents_enabled}
  listen: "0.0.0.0:8443"

  # What your agents will have typed after wss://. The listener issues itself
  # a certificate for exactly these names, so an agent that dialled anything
  # not in the list fails the handshake and says so.
  #
  # **The first one is the address this Controller hands out.** Every "add a
  # host" command and every agent it installs over SSH is told to dial it, so
  # it has to be the name your *other* machines can reach this one at. The
  # loopback entries after it are for a browser and a local agent on this
  # machine.
  server_names: [${names%, }]

  # The one directory worth backing up: the CA private key, the enrolment
  # registry and the audit log. Losing it means re-approving every host.
  state_dir: ${STATE_DIR}

  # Where bystack-manager leaves the fleet's signed agents after it has
  # updated this Controller (ADR-0018, phase two). Not a preference: it is one
  # half of a conversation with a root process, and pointing it elsewhere
  # makes an update that works and a fleet that never hears about it.
  releases_dir: ${HOME_DIR}/releases

  # The two files this Controller and its updater talk through. root:bystack
  # 1770 -- this account writes its own request and cannot delete root's
  # report of how the update went.
  manager_ipc_dir: ${HOME_DIR}/ipc

local_agent:
  # So this machine appears on its own map. It needs the socket to be readable
  # by the ${SERVICE_USER} account, which is the SupplementaryGroups line in
  # the unit; without it the Controller is fine and the map is simply empty.
  docker_socket: ${docker_socket}

api:
  # Loopback by default, on purpose: nothing here asks a browser for a
  # password, so the Controller does not put itself on your network without
  # being told to. Reach it over \`ssh -L\`, or set 0.0.0.0 on a network you
  # trust -- or put your own login page in front of it.
  host: ${bind_host}
  port: ${bind_port}
  cors_origins: []

log_level: INFO
EOF
	chmod 0644 "$CONF"
	echo "==> wrote ${CONF}"
fi

# --------------------------------------------------------------------------
# The units
# --------------------------------------------------------------------------
#
# Four, and the shape of them is the whole safety argument (ADR-0018):
#
#   bystack-controller.service        the Controller. Can write two
#                                     directories inside ${HOME_DIR} and
#                                     nothing else on the machine
#   bystack-manager.path              sees update.intent appear. Takes no
#                                     arguments, so there is no shape in which
#                                     the process being replaced parameterises
#                                     what root does
#   bystack-manager.service           Type=oneshot, root, network. Fetch,
#                                     verify, smoke-test, stop, swap, start,
#                                     poll for the version it installed, exit.
#                                     No resident root daemon
#   bystack-manager-rollback.service  the one failure the oneshot cannot
#                                     handle: not being there. Conditioned on
#                                     the probation file, at boot
#
# `packaging/systemd/` holds the same four with the reasoning at length. They
# are written here rather than fetched because this script is one file curl'd
# onto a machine.

cat >"${UNIT_DIR}/bystack-controller.service" <<EOF
[Unit]
Description=ByStack Controller
Documentation=https://github.com/${REPO}
# Not Requires=. A Controller with no local engine is an ordinary deployment,
# not a fault: it says so on first run and every enrolled host is unaffected.
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=exec
ExecStart=${HOME_DIR}/bin/bystack-controller --config ${CONF}
Restart=always
RestartSec=5
# serve() installs its own SIGTERM handler so a stop is a stop rather than a
# kill, and so the local agent's socket is cleaned up on the way out.
KillSignal=SIGTERM
TimeoutStopSec=30

User=${SERVICE_USER}
Group=${SERVICE_USER}
${supplementary}
StateDirectory=bystack
StateDirectoryMode=0700
WorkingDirectory=${STATE_DIR}

# ProtectSystem=strict makes /opt read-only, which is what we want for the
# directory holding the executable this process runs out of. Two exceptions,
# both narrow: ipc/ is how the update button reaches root, and cache/ is where
# the zipapp unpacks itself. Neither holds anything that is executed.
# releases/ is deliberately absent: the Controller reads it, root writes it.
ReadWritePaths=${HOME_DIR}/ipc ${HOME_DIR}/cache
Environment=SHIV_ROOT=${HOME_DIR}/cache

NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectClock=yes
ProtectHostname=yes
ProtectKernelLogs=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RemoveIPC=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF

cat >"${UNIT_DIR}/bystack-manager.path" <<EOF
[Unit]
Description=Watch for a ByStack Controller update request
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0018-controller-self-update.md

[Path]
# PathExists= rather than PathChanged=. \`bystack-manager apply\` deletes the
# file before it does anything else, so "it is there" is exactly the condition
# -- and it re-fires correctly for a second request that writes an identical
# file.
PathExists=${HOME_DIR}/ipc/update.intent
Unit=bystack-manager.service

[Install]
WantedBy=paths.target
EOF

# A machine installed from a mirror keeps updating from that mirror. The
# alternative is an air-gapped Controller that installs perfectly and then
# reports that it cannot reach github.com the first time somebody presses the
# update button.
manager_mirror=""
if [ -n "${BYSTACK_RELEASE_BASE:-}" ]; then
	manager_mirror="Environment=BYSTACK_RELEASE_BASE=${BYSTACK_RELEASE_BASE%/}"
fi

cat >"${UNIT_DIR}/bystack-manager.service" <<EOF
[Unit]
Description=Install a signed ByStack Controller release
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0018-controller-self-update.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${HOME_DIR}/bin/bystack-manager apply
User=root
Environment=BYSTACK_HOME=${HOME_DIR}
Environment=BYSTACK_REPO=${REPO}
${manager_mirror}

# Long enough for a slow uplink, plus three minutes of probation, plus the
# fleet's artifacts afterwards. A timeout that fired mid-probation would kill
# the process holding the rollback -- survivable, because the boot-time unit
# picks it up, and still not a thing to arrange on purpose.
TimeoutStartSec=1800

# No PrivateNetwork: this is the one component that pulls, and it pulls
# because the Controller has no parent to push to it. Everything it downloads
# is refused unless it is signed by a key compiled into the binary, so the
# network it has buys an attacker the ability to withhold an update.
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes
ProtectClock=yes
ProtectHostname=yes
ProtectKernelLogs=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RemoveIPC=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ProtectSystem=full
ReadWritePaths=${HOME_DIR}

[Install]
WantedBy=multi-user.target
EOF

cat >"${UNIT_DIR}/bystack-manager-rollback.service" <<EOF
[Unit]
Description=Undo an interrupted ByStack Controller update
Documentation=https://github.com/${REPO}/blob/main/docs/adr/0018-controller-self-update.md
# Conditioned on the file rather than run unconditionally: on every ordinary
# boot of every ordinary machine, this unit does not start at all.
ConditionPathExists=${HOME_DIR}/state/probation
# After, not Requires. The Controller starting is what this is deciding about,
# and a rollback that ran before it would be undoing a swap on the strength of
# a service that had not been given the chance to start.
After=bystack-controller.service
Wants=bystack-controller.service

[Service]
Type=oneshot
ExecStart=${HOME_DIR}/bin/bystack-manager rollback
User=root
Environment=BYSTACK_HOME=${HOME_DIR}
TimeoutStartSec=300

NoNewPrivileges=yes
PrivateNetwork=yes
ProtectHome=yes
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=${HOME_DIR}

[Install]
WantedBy=multi-user.target
EOF

for unit in $(units); do
	chmod 0644 "${UNIT_DIR}/${unit}"
done
systemctl daemon-reload

# --------------------------------------------------------------------------
# Start it
# --------------------------------------------------------------------------

if [ "$start" = false ]; then
	echo
	echo "installed, not started (--no-start). When you are ready:"
	echo "  systemctl enable --now bystack-controller bystack-manager.path bystack-manager-rollback"
	exit 0
fi

# The path unit and the rollback are enabled but not started as a pair with the
# Controller: one waits on a file that does not exist yet, and the other
# refuses to run unless a swap was interrupted.
systemctl enable bystack-manager.path bystack-manager-rollback.service >/dev/null 2>&1 || true
systemctl start bystack-manager.path
systemctl enable bystack-controller >/dev/null 2>&1 || true
systemctl restart bystack-controller

health="http://127.0.0.1:${bind_port}/api/v1/healthz"

# Neither is present only on an offline install that used --binary and
# --manager, since a download needs one. Reported as "not checked" rather than
# waited out and failed: a machine that has no way to ask is not a machine
# whose Controller did not answer.
have_probe=true
if command -v curl >/dev/null 2>&1; then
	probe() { curl -fsS --max-time 3 "$1" 2>/dev/null; }
elif command -v wget >/dev/null 2>&1; then
	probe() { wget -qO- --timeout=3 "$1" 2>/dev/null; }
else
	have_probe=false
	probe() { return 1; }
fi

# The version is part of the question, for the reason the manager's probation
# check asks it: "the port answers" is satisfied by a Controller that is still
# the old one, and by anything else that happens to hold that port.
answered=false
i=0
while [ "$have_probe" = true ] && [ $i -lt 60 ]; do
	body="$(probe "$health" || true)"
	case "$body" in
	*"\"version\":\"${installing}\""* | *"\"version\": \"${installing}\""*)
		answered=true
		break
		;;
	esac
	sleep 1
	i=$((i + 1))
done

echo
if [ "$answered" = true ]; then
	echo "ByStack ${installing} is running."
	echo
	if [ "$bind_host" = "127.0.0.1" ] || [ "$bind_host" = "::1" ]; then
		echo "  Dashboard:  http://127.0.0.1:${bind_port}"
		echo "  From your own machine, if this is a server you reach over ssh:"
		# `hostname` is not on a minimal host and neither is `hostnamectl`;
		# a placeholder beats an error message inside the one line an
		# operator is meant to copy.
		here="$(hostname -f 2>/dev/null || hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo your-server)"
		echo "    ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:${bind_port} you@${here}"
		echo "    then open http://127.0.0.1:8080"
	else
		echo "  Dashboard:  http://${bind_host}:${bind_port} (bound to ${bind_host})"
		echo "  Nothing here asks a browser for a password. Keep it on a network you"
		echo "  own, or put your own login page in front of it."
	fi
	echo
	if [ "$agents_enabled" = true ]; then
		echo "  The fleet listener is on. Add servers from the dashboard: Add host"
		echo "  gives you a one-line command to paste on each of them."
	else
		echo "  To manage other servers, set \`agents.enabled: true\` in ${CONF},"
		echo "  put their route to this machine in \`server_names\`, and restart."
	fi
	echo
	echo "  Updates are a button: Hosts -> Update system. It pulls the next signed"
	echo "  release, checks it against a key that is not on this machine, and puts"
	echo "  the old one back if the new one does not answer. From a terminal:"
	echo "    sudo ${HOME_DIR}/bin/bystack-manager request"
	echo "    ${HOME_DIR}/bin/bystack-manager status"
elif [ "$have_probe" = false ]; then
	echo "Installed. There is no curl or wget here to ask it whether it came up,"
	echo "so check it yourself:"
	echo
	echo "  systemctl status bystack-controller"
	echo "  Dashboard: http://${bind_host}:${bind_port}"
else
	echo "bystack-controller is installed but has not answered on ${health} yet."
	echo
	echo "  systemctl status bystack-controller"
	echo "  journalctl -u bystack-controller -n 50"
	exit 1
fi
