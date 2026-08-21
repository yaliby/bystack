#!/usr/bin/env bash
#
# Build the Controller as one file, once per architecture.
#
#     scripts/build-controller.sh                     # both architectures
#     scripts/build-controller.sh --arch x86_64
#     scripts/build-controller.sh --python-version 3.12
#
# What comes out is `dist/bystack-controller-<arch>`: a zipapp carrying the
# Controller, its dependencies, the agent binary for that architecture and the
# built dashboard. `scripts/sign-agent.py` signs it, `bystack-manager` installs
# it, and installing it is a `rename` (ADR-0018).
#
# ## Why one file, when the Controller is a Python package
#
# Because the update has to be *offline and atomic*, and a virtualenv is
# neither. `pip install --upgrade` into a live venv needs the network at the
# worst possible moment, half-applies when it fails, and cannot be undone by
# putting an inode back. A single file can be renamed over in one syscall and
# renamed back the same way, which is what makes the probation in
# `agent/manager/src/apply.rs` able to promise a rollback rather than attempt
# one.
#
# It also makes the Controller the same *kind* of thing as the agent, so one
# signing tool, one manifest format and one key set cover both -- which was the
# whole premise of ADR-0018.
#
# ## The interpreter is pinned, and that is the cost
#
# A zipapp carries wheels, and wheels with compiled extensions are built for
# one CPython ABI. `pydantic-core` and `uvloop` ship `cp312`-tagged wheels, so
# an artifact assembled from them imports under 3.12 and nothing else.
#
# So a release pins one minor version, and it is the floor `backend/pyproject.toml`
# already declares. The host needs a `python3.12` on its PATH; the shebang says
# so, and `bystack-manager` runs `--version` on the downloaded artifact before
# it stops anything, so a host that cannot run it refuses the upgrade instead
# of taking one.
#
# The alternative -- bundling an interpreter with PyInstaller -- removes that
# constraint and costs a native build per architecture, an emulated aarch64
# release job, and a second packaging format to keep working. Worth revisiting
# when the constraint actually bites; not worth paying up front.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${REPO}/dist"
ARTIFACT="bystack-controller"

#: The CPython the artifact is assembled for. `requires-python` in
#: backend/pyproject.toml is the floor and this is that floor: building for the
#: newest available would produce a release that will not start on a host
#: running what the install instructions asked for.
PYTHON_VERSION="3.12"

ARCHES=(x86_64 aarch64)

die() {
	echo "build-controller.sh: $*" >&2
	exit 1
}

#: The manylinux tags a wheel may carry for one architecture.
#:
#: Listed rather than derived, because pip matches `--platform` against a
#: wheel's tag *literally*: `manylinux2014_x86_64` does not match a wheel named
#: `manylinux_2_17_x86_64` even though the two mean the same thing. Getting
#: this wrong does not produce a broken artifact -- it produces "is not a
#: supported wheel on this platform" and no artifact at all, which is the right
#: way for it to fail.
platforms_for() {
	case "$1" in
	x86_64) echo "manylinux_2_17_x86_64 manylinux2014_x86_64 manylinux_2_28_x86_64 manylinux_2_34_x86_64" ;;
	aarch64) echo "manylinux_2_17_aarch64 manylinux2014_aarch64 manylinux_2_28_aarch64 manylinux_2_34_aarch64" ;;
	*) return 1 ;;
	esac
}

arches=()
while [ $# -gt 0 ]; do
	case "$1" in
	--arch) arches+=("${2:?--arch needs an architecture}"); shift 2 ;;
	--python-version) PYTHON_VERSION="${2:?--python-version needs a minor version}"; shift 2 ;;
	--out) OUT="${2:?--out needs a directory}"; shift 2 ;;
	-h | --help)
		sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'
		exit 0
		;;
	*) die "unknown argument $1" ;;
	esac
done
[ ${#arches[@]} -gt 0 ] || arches=("${ARCHES[@]}")

python="${BYSTACK_PYTHON:-}"
if [ -z "$python" ]; then
	for candidate in "${REPO}/.venv/bin/python" "${REPO}/backend/.venv/bin/python" python3; do
		if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
			python="$candidate"
			break
		fi
	done
fi
[ -n "$python" ] || die "no python to build with. Set BYSTACK_PYTHON."

# `shiv` and `build`, in a directory of their own. Not installed into the
# checkout's venv: that venv is what the test suite imports out of, and a build
# tool that lands in it is a dependency nobody declared.
tools="${REPO}/.bystack-test/build-tools"
if [ ! -x "${tools}/bin/shiv" ]; then
	echo "==> build tools"
	"$python" -m venv "$tools"
	"${tools}/bin/pip" install --quiet --upgrade pip shiv build
fi

version="$("$python" -c "
import pathlib, re
text = pathlib.Path('${REPO}/backend/pyproject.toml').read_text()
print(re.search(r'^version = \"([^\"]+)\"', text, re.M).group(1))
")"
[ -n "$version" ] || die "cannot read the version out of backend/pyproject.toml"

mkdir -p "$OUT"

echo "==> bystack ${version}, CPython ${PYTHON_VERSION}"

for arch in "${arches[@]}"; do
	platforms="$(platforms_for "$arch")" || die "no platform tags known for ${arch}"

	# One wheel per architecture, each carrying the matching agent
	# (`backend/hatch_build.py`). Built here rather than reused from `wheels/`
	# so that what goes into the zipapp is the wheel for *this* architecture
	# and not whichever one happened to be lying about.
	[ -f "${OUT}/bystack-agent-${arch}" ] ||
		die "no ${OUT}/bystack-agent-${arch}. The Controller carries the agent it
  spawns for its own engine; run scripts/build-agent.sh first."
	[ -d "${REPO}/frontend/dist" ] ||
		die "no frontend/dist. The Controller carries the dashboard; run
  \`npm ci && npm run build\` in frontend/ first."

	wheels="${REPO}/.bystack-test/wheel-${arch}"
	rm -rf "$wheels"
	echo "==> wheel (${arch})"
	(cd "${REPO}/backend" && BYSTACK_TARGET_ARCH="$arch" "${tools}/bin/python" -m build \
		--wheel --outdir "$wheels") | sed 's/^/    /'

	wheel="$(ls "${wheels}"/*.whl)"
	case "$wheel" in
	*py3-none-any*) die "the wheel for ${arch} carries no agent; is dist/bystack-agent-${arch} executable?" ;;
	esac

	echo "==> zipapp (${arch})"
	platform_args=()
	for platform in $platforms; do platform_args+=(--platform "$platform"); done

	# `--abi` is given three times on purpose: `cp3XX` for the extensions that
	# build per-version, `abi3` for the ones that build once (cryptography), and
	# `none` for the pure-Python majority. Leave any of them out and pip refuses
	# a dependency that was perfectly installable.
	"${tools}/bin/shiv" \
		--console-script bystack \
		--python "/usr/bin/env python${PYTHON_VERSION}" \
		--compressed \
		--reproducible \
		-o "${OUT}/${ARTIFACT}-${arch}" \
		"$wheel" \
		"${platform_args[@]}" \
		--python-version "$PYTHON_VERSION" \
		--implementation cp \
		--abi "cp${PYTHON_VERSION//./}" --abi abi3 --abi none \
		--only-binary=:all: |
		tail -1 | sed 's/^/    /'

	chmod +x "${OUT}/${ARTIFACT}-${arch}"

	# A signature made for the *previous* build is worse than a stale checksum,
	# because it is cryptographically valid and describes bytes that are no
	# longer there. Same reason build-agent.sh deletes these.
	rm -f "${OUT}/${ARTIFACT}-${arch}.manifest" "${OUT}/${ARTIFACT}-${arch}.manifest.sig"
done

# --------------------------------------------------------------------------
# It has to answer for itself
# --------------------------------------------------------------------------
#
# Only the native architecture can be run, and only when this machine has the
# interpreter the artifact was pinned to. That is enough: both architectures
# were assembled by one run from one wheel-building path, so an artifact that
# cannot start is caught here on whichever arch this machine happens to be.
#
# This is the check that would otherwise be performed by a fleet's Controller,
# at three in the morning, as a rollback.
native="$(uname -m)"
if command -v "python${PYTHON_VERSION}" >/dev/null 2>&1 && [ -f "${OUT}/${ARTIFACT}-${native}" ]; then
	echo "==> ${ARTIFACT}-${native} --version"
	# SHIV_ROOT into a scratch directory: the first run of a zipapp unpacks its
	# site-packages, and doing that into the builder's home directory leaves a
	# cache that the next build silently reuses.
	reported="$(SHIV_ROOT="$(mktemp -d)" "${OUT}/${ARTIFACT}-${native}" --version | awk '{print $NF}')"
	[ "$reported" = "$version" ] ||
		die "the tree says ${version} and the artifact reports ${reported}"
	echo "    ${reported}"
else
	echo "==> not run: this is ${native} and needs python${PYTHON_VERSION} to try" >&2
fi

(
	cd "$OUT"
	names=()
	for arch in "${arches[@]}"; do names+=("${ARTIFACT}-${arch}"); done
	sha256sum "${names[@]}" >"SHA256SUMS.${ARTIFACT}"
)

echo
ls -l "${OUT}/${ARTIFACT}"-* >&2
