#!/usr/bin/env bash
#
# Build the agent as a static binary, once per architecture.
#
#     scripts/build-agent.sh                      # every target, in a container
#     scripts/build-agent.sh --target aarch64-unknown-linux-musl
#     scripts/build-agent.sh --native             # this host's toolchain
#
# What comes out is `dist/bystack-agent-<arch>` and a `SHA256SUMS` beside it.
# One file per architecture and nothing else: an agent is installed by copying
# a single file onto a machine (`agent/src/main.rs`), and the whole reason it
# has no config file is that there is nothing to deploy alongside it. A
# tarball with a README in it would put that back.
#
# ## Why musl, and why in a container
#
# The agent runs on hardware the operator already owns -- a NAS, a Pi, a VPS
# from 2019 -- and glibc's symbol versioning means a binary linked against the
# build machine's glibc refuses to start on anything older. That is the single
# most common way a "static" binary is not one. musl links the whole libc in,
# so the only thing the kernel needs to offer is the syscall interface.
#
# `ring` (and therefore rustls, and therefore the agent) compiles C, so a musl
# target needs a musl-targeting C compiler -- which is the one claim in
# `agent/Cargo.toml` that deserves a footnote: choosing `ring` over `aws-lc-rs`
# avoids needing *cmake and a full C toolchain per architecture*, not a C
# compiler outright. A cross image supplies exactly that and nothing else has
# to be installed on anyone's laptop.
#
# The binary is streamed out over stdout rather than written into a mounted
# directory, deliberately. A container writing into a bind mount produces
# root-owned files under rootful Docker and user-owned ones under rootless
# Podman, and the difference surfaces days later as a permission error in an
# unrelated build. The redirect happens on the host, so the file belongs to
# whoever ran this either way.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${REPO}/dist"

# musl for every target, for the reason above. Two architectures because those
# are the two anyone runs Docker on; adding a third is a line here plus an
# image tag, and nothing else in the tree knows how many there are.
DEFAULT_TARGETS=(x86_64-unknown-linux-musl aarch64-unknown-linux-musl)

#: The cross toolchain, per target. Overridable because pinning a third party's
#: `:latest` into a release process is a supply-chain decision an operator may
#: reasonably want to make differently -- and because an air-gapped build has
#: to be able to point this at a local mirror.
image_for() {
	case "$1" in
	x86_64-unknown-linux-musl) echo "${BYSTACK_BUILDER_X86_64:-docker.io/messense/rust-musl-cross:x86_64-musl}" ;;
	aarch64-unknown-linux-musl) echo "${BYSTACK_BUILDER_AARCH64:-docker.io/messense/rust-musl-cross:aarch64-musl}" ;;
	*) return 1 ;;
	esac
}

#: `x86_64-unknown-linux-musl` -> `x86_64`. The architecture is the only part
#: of a target triple an operator recognises, and the installer matches on
#: `uname -m`, which prints exactly this.
arch_of() { echo "${1%%-*}"; }

targets=()
native=false
while [ $# -gt 0 ]; do
	case "$1" in
	--target)
		targets+=("${2:?--target needs a triple}")
		shift 2
		;;
	--native)
		native=true
		shift
		;;
	--out)
		OUT="${2:?--out needs a directory}"
		shift 2
		;;
	-h | --help)
		sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'
		exit 0
		;;
	*)
		echo "build-agent.sh: unknown argument $1" >&2
		exit 2
		;;
	esac
done
[ ${#targets[@]} -gt 0 ] || targets=("${DEFAULT_TARGETS[@]}")

# Podman first. Not a preference between the two: rootless is the default on
# the distributions that ship Podman, and a build that needs no daemon and no
# group membership is the one to try before the one that needs both.
engine=""
for candidate in podman docker; do
	if command -v "$candidate" >/dev/null 2>&1 && "$candidate" info >/dev/null 2>&1; then
		engine="$candidate"
		break
	fi
done

if [ "$native" = false ] && [ -z "$engine" ]; then
	echo "build-agent.sh: no working podman or docker." >&2
	echo "  Install one, or use --native if this host already has a musl toolchain" >&2
	echo "  for the target (rustup target add …, plus <arch>-linux-musl-gcc)." >&2
	exit 1
fi

mkdir -p "$OUT"

# What the binary must be, checked rather than asserted. A dynamically linked
# "static" binary is the exact failure this script exists to prevent, and it is
# invisible until it reaches a machine with a different libc -- so the check
# runs where the binary was made, while a toolchain that can see into it is
# still in scope.
#
# A missing `readelf` is a failure rather than a skip. Skipping would make this
# a check that any future base image can retire by dropping binutils, and the
# whole point is that nothing else in the pipeline would notice.
# shellcheck disable=SC2016  # $BIN is the container's variable, not ours
assert_static='
	command -v readelf >/dev/null 2>&1 || {
		echo "build-agent.sh: no readelf; cannot verify $BIN is static" >&2
		exit 1
	}
	if readelf -l "$BIN" | grep -q INTERP; then
		echo "build-agent.sh: $BIN wants a dynamic loader; it is not static" >&2
		exit 1
	fi
'

build_in_container() {
	local target="$1" image
	image="$(image_for "$target")" || {
		echo "build-agent.sh: no builder image known for $target." >&2
		echo "  Build it with --native, or set BYSTACK_BUILDER_* for a target this knows." >&2
		exit 2
	}

	# One named volume per target, holding both the cargo registry and the
	# target directory. Engine-managed, so it has none of the ownership
	# problems a bind mount would, and it is what makes the second build of
	# the day take seconds instead of four minutes.
	local cache="bystack-agent-build-${target}"

	echo "==> $target via $engine ($image)" >&2
	# `label=disable` rather than a `:z` mount. On an SELinux host the source
	# bind mount is unreadable without one of the two, and `:z` gets there by
	# relabelling the checkout on the host -- a persistent change to files
	# this script was only asked to read. Turning the separation off for one
	# short-lived container that reads our own source is the smaller thing to
	# do. Both engines accept the flag, and it is a no-op where SELinux is not
	# enforcing.
	"$engine" run --rm \
		--security-opt label=disable \
		-v "${REPO}:/src:ro" \
		-v "${cache}:/cache" \
		-e CARGO_TARGET_DIR=/cache/target \
		-e BIN="/cache/target/${target}/release/bystack-agent" \
		-w /src/agent \
		"$image" \
		sh -euc "
			 # Moving CARGO_HOME onto the cache volume also moves cargo's
			 # config file, and that file is where a cross image says which
			 # linker to use for its target. Losing it does not fail the
			 # build -- it silently links with the *host* ld, which happens
			 # to work for x86_64 and produces 'Relocations in generic ELF'
			 # for everything else. The image's own answer is copied across
			 # rather than restated here, because the prefix its toolchain
			 # uses is the image's business and not ours.
			 mkdir -p /cache/cargo
			 for name in config config.toml; do
				 from=\"\${CARGO_HOME:-\$HOME/.cargo}/\$name\"
				 [ -f \"\$from\" ] && cp -n \"\$from\" /cache/cargo/ || true
			 done
			 export CARGO_HOME=/cache/cargo

			 cargo build --release --locked --target ${target} >&2
			 ${assert_static}
			 cat \"\$BIN\"" \
		>"${OUT}/bystack-agent-$(arch_of "$target")"
}

build_native() {
	local target="$1"
	echo "==> $target natively" >&2
	(cd "${REPO}/agent" && cargo build --release --locked --target "$target" >&2)
	local bin="${REPO}/agent/target/${target}/release/bystack-agent"
	BIN="$bin" sh -euc "${assert_static}"
	cp "$bin" "${OUT}/bystack-agent-$(arch_of "$target")"
}

for target in "${targets[@]}"; do
	if [ "$native" = true ]; then
		build_native "$target"
	else
		build_in_container "$target"
	fi
	chmod +x "${OUT}/bystack-agent-$(arch_of "$target")"
done

# A signature made for the *previous* build of this architecture is worse than
# a stale checksum, because it is cryptographically valid: it verifies under
# the fleet's key and describes bytes that are no longer there. Every consumer
# does catch it -- `releases.py` compares the digest to the file beside it, and
# so does `install-agent.sh` -- but each of them catches it late, on a
# Controller or on a host, as a puzzling refusal. Deleting it here means the
# only manifest in this directory is one `scripts/release-agent.sh` made for
# the binary that is actually sitting next to it.
for target in "${targets[@]}"; do
	rm -f "${OUT}/bystack-agent-$(arch_of "$target")".manifest \
		"${OUT}/bystack-agent-$(arch_of "$target")".manifest.sig
done

# Written last and covering only what this run produced. A checksum file that
# accumulated lines from previous runs would vouch for binaries nobody built
# today, which is worse than having none.
(
	cd "$OUT"
	names=()
	for target in "${targets[@]}"; do names+=("bystack-agent-$(arch_of "$target")"); done
	sha256sum "${names[@]}" >SHA256SUMS
)

echo >&2
ls -l "$OUT" >&2
