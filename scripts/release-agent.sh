#!/usr/bin/env bash
#
# Sign a release, on the machine that holds the key, and attach the signatures.
#
#     scripts/release-agent.sh v0.4.0                 # fetch the published tag, sign
#     scripts/release-agent.sh v0.4.0 --attach        # ... and upload the signatures
#     scripts/release-agent.sh v0.4.0 --local         # sign what build-agent.sh made
#
# ## Why this is not a CI job
#
# The private key is the most valuable secret in this project: it is the one
# thing that can make a binary every host in every fleet will install as root.
# In CI secrets it would be reachable by a workflow change, which makes the CI
# account worth more than the key. So the key stays on the machine that cuts
# releases, and the pipeline is split across the boundary that decision draws:
#
#   1. `.github/workflows/release.yml` builds and publishes the binaries. No
#      key, nothing to steal, and the artifacts it produces are worthless to
#      an attacker who cannot sign them.
#   2. This script, run by hand afterwards, signs those exact bytes and
#      attaches the manifests.
#
# Between the two, `install-agent.sh` falls back to `SHA256SUMS` and says so,
# and the Controller declines to push a release it has no signature for. The
# window is visible in both places rather than silently unverified, which is
# the property that makes it safe to have a window at all (ADR-0017).
#
# ## What it checks that running the two scripts by hand does not
#
# Signing is one command; the failures are all in what surrounds it, and every
# one of them produces a *valid signature over the wrong thing* -- which no
# amount of verification downstream can catch, because downstream it verifies.
#
#   - the version in the manifest is the version the binary reports. Get this
#     wrong and the agent's anti-downgrade check is comparing against a number
#     that was never true: a fleet installs "0.4.0", reports 0.3.0, and refuses
#     the real 0.4.0 forever after as not-an-upgrade.
#   - the signature verifies under the key `agent/keys/` holds -- the key the
#     *fleet* trusts, not the key we happened to sign with. Signing a release
#     with a freshly minted key produces artifacts nothing will install, and
#     the only place that is cheap to notice is here.
#   - and under openssl, which is the implementation `install-agent.sh` uses.
#     Two verifiers, because the one bug that would be invisible to a single
#     one is a manifest that only one of them can read.
#   - both architectures, or neither. A tag with one signed artifact is a
#     fleet that upgrades on half its hosts and refuses on the other half.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GH_REPO="${BYSTACK_REPO:-yaliby/bystack}"
KEY="${BYSTACK_SIGNING_KEY:-${HOME}/.bystack/release.key}"
OUT="${REPO_DIR}/dist"
ARCHES=(x86_64 aarch64)

tag=""
local_build=false
attach=false

die() {
	echo "release-agent.sh: $*" >&2
	exit 1
}

while [ $# -gt 0 ]; do
	case "$1" in
	--key) KEY="${2:?--key needs a path}"; shift 2 ;;
	--out) OUT="${2:?--out needs a directory}"; shift 2 ;;
	--local) local_build=true; shift ;;
	--attach) attach=true; shift ;;
	-h | --help)
		sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'
		exit 0
		;;
	-*) die "unknown option $1" ;;
	*) tag="$1"; shift ;;
	esac
done

[ -n "$tag" ] || die "which tag? e.g. release-agent.sh v0.4.0"
case "$tag" in
v*) ;;
*) die "the tag is the one CI published, and those start with v: got '$tag'" ;;
esac
#: `v0.4.0` -> `0.4.0`. The manifest carries the version the binary reports,
#: and `--version` does not print the `v`.
version="${tag#v}"

[ -f "$KEY" ] || die "no signing key at ${KEY}.
  It is minted by \`scripts/sign-agent.py keygen\` and lives on this machine
  and nowhere else -- not on the Controller, not in CI. If this is a new
  machine, the key is on the old one; if it is lost, a rotation is two
  releases (agent/keys/README.md)."

python="${BYSTACK_PYTHON:-}"
if [ -z "$python" ]; then
	for candidate in "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/backend/.venv/bin/python" python3; do
		if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
			if "$candidate" -c 'import cryptography' 2>/dev/null; then
				python="$candidate"
				break
			fi
		fi
	done
fi
[ -n "$python" ] || die "no python with \`cryptography\` (which the Controller already depends on).
  Set BYSTACK_PYTHON, or \`pip install cryptography\`."

# --------------------------------------------------------------------------
# 0. The key this is about to sign with must be one the fleet accepts
# --------------------------------------------------------------------------
#
# Before anything is fetched or written, because a mismatch here makes every
# step after it worthless and the whole run wasted bandwidth.
echo "==> keys"
"${REPO_DIR}/scripts/check-release-keys.sh" | sed 's/^/    /'

# --------------------------------------------------------------------------
# 1. The bytes
# --------------------------------------------------------------------------

mkdir -p "$OUT"

if [ "$local_build" = true ]; then
	echo "==> signing the local build in ${OUT}"
	for arch in "${ARCHES[@]}"; do
		[ -f "${OUT}/bystack-agent-${arch}" ] ||
			die "no ${OUT}/bystack-agent-${arch}. Run scripts/build-agent.sh first."
	done
else
	base="https://github.com/${GH_REPO}/releases/download/${tag}"
	echo "==> fetching ${tag} from ${GH_REPO}"
	# Downloaded rather than taken from `dist/`, deliberately. What gets signed
	# has to be the bytes the fleet will actually receive, and a local build
	# from the same commit is *not* the same file: a different toolchain, a
	# different cache, a dirty tree. Signing a local rebuild would produce a
	# manifest whose digest matches nothing anyone can download.
	for arch in "${ARCHES[@]}"; do
		curl -fsSL "${base}/bystack-agent-${arch}" -o "${OUT}/bystack-agent-${arch}" ||
			die "no bystack-agent-${arch} at ${tag}. Has the release workflow finished?"
	done
	curl -fsSL "${base}/SHA256SUMS" -o "${OUT}/SHA256SUMS.published" ||
		die "no SHA256SUMS at ${tag}"

	# Not a security control and not treated as one -- it came over the same
	# connection as the binaries. It is here because a truncated download is
	# the one failure that would otherwise be signed, published, and discovered
	# by a fleet.
	echo "==> checksums as published"
	for arch in "${ARCHES[@]}"; do
		want="$(awk -v f="bystack-agent-${arch}" '$2 == f || $2 == "*"f {print $1}' "${OUT}/SHA256SUMS.published")"
		got="$(sha256sum "${OUT}/bystack-agent-${arch}" | cut -d' ' -f1)"
		[ -n "$want" ] || die "SHA256SUMS at ${tag} names no bystack-agent-${arch}"
		[ "$want" = "$got" ] || die "bystack-agent-${arch} does not match the published SHA256SUMS"
		echo "    bystack-agent-${arch}  ok"
	done
fi

chmod +x "${OUT}"/bystack-agent-*

# --------------------------------------------------------------------------
# 2. The version in the manifest is the version the binary reports
# --------------------------------------------------------------------------
#
# The tag is the source of the version -- one number for everything published
# together (CHANGELOG.md) -- and this is where that claim is checked against
# the only thing that can contradict it.
#
# Only the native architecture can be asked, because running the other one
# needs an emulator this script is not going to require. That is enough: both
# binaries were built from one tag by one workflow, so a tree whose version was
# never bumped is caught here on whichever arch this machine happens to be.
native="$(uname -m)"
case "$native" in
amd64) native=x86_64 ;;
arm64) native=aarch64 ;;
esac

echo "==> version"
checked=false
for arch in "${ARCHES[@]}"; do
	if [ "$arch" = "$native" ]; then
		reported="$("${OUT}/bystack-agent-${arch}" --version | awk '{print $NF}')"
		[ "$reported" = "$version" ] ||
			die "the tag says ${version} and bystack-agent-${arch} reports ${reported}.
  Signing it as ${version} would give every host a manifest that disagrees with
  the binary it describes, and the agent's anti-downgrade check reads the
  binary. Fix the version in the tree and re-cut the tag."
		echo "    bystack-agent-${arch} reports ${reported}"
		checked=true
	fi
done
if [ "$checked" = false ]; then
	echo "    no ${native} artifact in this release, so nothing here can be run;"
	echo "    taking ${version} from the tag."
fi

# --------------------------------------------------------------------------
# 3. Sign
# --------------------------------------------------------------------------
#
# One timestamp for the whole release rather than one per invocation. The
# architectures of a release were published together and there is no fact
# `released_at` could be reporting that makes them differ by the seconds this
# loop takes.
released_at="$(date -u +%s)"

echo "==> signing"
for arch in "${ARCHES[@]}"; do
	"$python" "${REPO_DIR}/scripts/sign-agent.py" sign "${OUT}/bystack-agent-${arch}" \
		--key "$KEY" --version "$version" --arch "$arch" --released-at "$released_at" |
		sed 's/^/    /'
done

# --------------------------------------------------------------------------
# 4. Verify as the fleet would, twice, with two implementations
# --------------------------------------------------------------------------

echo "==> verifying against agent/keys/ (what a host will accept)"
for arch in "${ARCHES[@]}"; do
	# No `--key`: the default is `agent/keys/*.pub`, which is the point. This
	# passes only if the key just used to sign is one the fleet was built to
	# trust, which is the failure that is otherwise found by a fleet.
	"$python" "${REPO_DIR}/scripts/sign-agent.py" verify "${OUT}/bystack-agent-${arch}" |
		sed 's/^/    /'
done

echo "==> verifying with openssl (what install-agent.sh will use)"
pem="$(mktemp)"
trap 'rm -f "$pem"' EXIT
sh "${REPO_DIR}/scripts/install-agent.sh" --keys >/dev/null ||
	die "install-agent.sh cannot decode its own keys"
for arch in "${ARCHES[@]}"; do
	verified=false
	# Every key in the installer's set, because that is what the installer
	# does: during a rotation more than one is legitimate and exactly one works.
	"$python" - "${REPO_DIR}" "$pem" <<-'PY'
		import re, subprocess, sys
		repo, out = sys.argv[1], sys.argv[2]
		text = open(f"{repo}/scripts/install-agent.sh").read()
		open(out, "w").write("\n".join(re.findall(
		    r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", text, re.S)))
	PY
	csplit -z -f "${pem}." -b '%d.pem' "$pem" '/BEGIN PUBLIC KEY/' '{*}' >/dev/null 2>&1 || cp "$pem" "${pem}.0.pem"
	for key in "${pem}."*.pem; do
		if openssl pkeyutl -verify -pubin -inkey "$key" -rawin \
			-in "${OUT}/bystack-agent-${arch}.manifest" \
			-sigfile "${OUT}/bystack-agent-${arch}.manifest.sig" >/dev/null 2>&1; then
			verified=true
			break
		fi
	done
	rm -f "${pem}."*.pem
	[ "$verified" = true ] ||
		die "openssl will not verify bystack-agent-${arch}.manifest against the
  keys in install-agent.sh, even though python did. The two implementations
  disagree, which means one of them is reading a different document."
	echo "    bystack-agent-${arch}.manifest  ok"
done

# --------------------------------------------------------------------------
# 5. Both, or neither
# --------------------------------------------------------------------------

for arch in "${ARCHES[@]}"; do
	for suffix in "" .manifest .manifest.sig; do
		[ -s "${OUT}/bystack-agent-${arch}${suffix}" ] ||
			die "bystack-agent-${arch}${suffix} is missing or empty; not attaching a partial release"
	done
done

echo
ls -l "${OUT}"/bystack-agent-*.manifest "${OUT}"/bystack-agent-*.manifest.sig

# --------------------------------------------------------------------------
# 6. Attach
# --------------------------------------------------------------------------

if [ "$attach" = false ]; then
	echo
	echo "Signed, not uploaded. Attach them with:"
	echo "  scripts/release-agent.sh ${tag} --local --attach"
	echo
	echo "That uses \`gh\` where it is installed and the REST API with GH_TOKEN where"
	echo "it is not. The four files are public artifacts either way -- the key that"
	echo "made them does not leave this machine, so they can also be attached by hand"
	echo "from anywhere."
	exit 0
fi

echo "==> attaching to ${tag}"

# Overwriting rather than refusing, in both paths below. The failure this run
# exists to prevent is a signature that does not match the artifact, and
# re-signing after fixing one is the ordinary way out; refusing to overwrite
# would leave the wrong one in place, which is the state this is for escaping.
if command -v gh >/dev/null 2>&1; then
	gh release upload "$tag" --repo "$GH_REPO" --clobber \
		"${OUT}"/bystack-agent-*.manifest "${OUT}"/bystack-agent-*.manifest.sig
else
	# No `gh`, and that should not be what stops a release being signed. The
	# machine this runs on is chosen for holding the key, not for having
	# GitHub's CLI installed -- and everything above already needed `curl`,
	# so the fallback adds no dependency that was not there.
	token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
	[ -n "$token" ] || die "no \`gh\`, and neither GH_TOKEN nor GITHUB_TOKEN is set,
  so the signatures cannot be uploaded. They are in ${OUT} and can be attached
  by hand from any machine -- they are public artifacts, and the key that made
  them stays here."

	api="https://api.github.com/repos/${GH_REPO}"
	release="$(curl -fsSL -H "Authorization: Bearer ${token}" "${api}/releases/tags/${tag}")" ||
		die "no published release for ${tag}. This signs what the release workflow
  published; if that has not finished, there is nothing to attach to yet."

	# The id, and the names already attached. Parsed with python rather than by
	# pattern: `upload_url` carries a `{?name,label}` template and an asset
	# named by an attacker is not a thing a regex should be deciding about.
	id="$("$python" -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<-EOF
		${release}
	EOF
	)"

	for file in "${OUT}"/bystack-agent-*.manifest "${OUT}"/bystack-agent-*.manifest.sig; do
		name="$(basename "$file")"

		# Delete first: the upload endpoint refuses a name that already exists
		# rather than replacing it, so this is what `--clobber` spells above.
		existing="$("$python" -c '
import json, sys
name = sys.argv[1]
for asset in json.load(sys.stdin):
    if asset["name"] == name:
        print(asset["id"])
' "$name" <<-EOF
			$(curl -fsSL -H "Authorization: Bearer ${token}" "${api}/releases/${id}/assets")
		EOF
		)"
		if [ -n "$existing" ]; then
			curl -fsSL -X DELETE -H "Authorization: Bearer ${token}" \
				"${api}/releases/assets/${existing}" >/dev/null
		fi

		curl -fsSL -X POST \
			-H "Authorization: Bearer ${token}" \
			-H "Content-Type: application/octet-stream" \
			--data-binary "@${file}" \
			"https://uploads.github.com/repos/${GH_REPO}/releases/${id}/assets?name=${name}" \
			>/dev/null || die "could not upload ${name}"
		echo "    ${name}"
	done
fi

echo
echo "${tag} is signed. A host installs it with the command the dashboard prints,"
echo "and every host already in the fleet can now be upgraded to it from Hosts."
