#!/usr/bin/env python3
"""Sign an agent release, so a fleet can install it without trusting the Controller.

    scripts/sign-agent.py keygen --out ~/.bystack/release.key
    scripts/sign-agent.py sign dist/bystack-agent-x86_64 --version 0.4.0 --key ~/.bystack/release.key
    scripts/sign-agent.py verify dist/bystack-agent-x86_64

What comes out of `sign` is two files beside the binary:

    bystack-agent-x86_64.manifest       the signed document
    bystack-agent-x86_64.manifest.sig   ed25519 over it, raw 64 bytes

Those are what the Controller distributes and what the agent checks. The
Controller holds no key and cannot make either of them, which is the whole
point: it can withhold an upgrade, send an old one, or send nothing, and it
cannot produce a binary any agent will run (ADR-0017).

## Why a document and not just a digest

The manifest carries `{name, version, arch, sha256, released_at}` and the
signature covers all of it. Signing the digest alone is the version of this
that looks equivalent and is not: the version would then come from the wire,
which makes it the sender's claim about a genuinely signed artifact -- an old
release with a known weakness, announced as new, against an anti-downgrade
check comparing a number the sender chose.

## Where the private key lives

On the machine that makes releases, and nowhere else. **Not on the
Controller**, which is the component this design deliberately does not trust,
and preferably not in CI: a workflow change signs anything, which makes the CI
account the most valuable target in the project. At this project's release
cadence, signing offline is both simpler and stronger. If it ever moves into
CI it belongs in a signing job separate from the build job, behind a protected
environment with a required review.

Needs `cryptography`, which the Controller already depends on. Nothing on a
managed host needs it: the agent verifies with `ring`, which it already carries
for TLS.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: The first line of the document, and the only place a format change may be
#: announced. An agent refuses a manifest whose magic it does not know, and
#: refuses unknown keys inside one -- so this is bumped, and the release that
#: bumps it is the release that teaches agents to read it. See
#: `agent/src/upgrade.rs`, `Manifest::parse`.
MAGIC = "bystack-manifest/1"

#: What is being signed. A key that ever signs a second artifact must not let
#: one be presented as the other, which is why the name is inside the document
#: rather than implied by the file it sits beside.
ARTIFACT = "bystack-agent"

#: `bystack-agent-x86_64` -> `x86_64`. The same spelling `uname -m` prints and
#: the agent's `std::env::consts::ARCH` reports, because those are the two
#: places it is compared against.
_ARCH = re.compile(rf"^{ARTIFACT}-(?P<arch>[a-z0-9_]+)$")


def cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        raise SystemExit(
            f"{out} already exists. Signing with a *new* key when you meant the old one "
            f"produces releases no fleet will install; pass --force if that is really what "
            f"you want."
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # The mode is set as the file is created rather than after: `open` then
    # `chmod` leaves a window in which the key a fleet trusts is on disk under
    # whatever umask happened to be in force.
    handle = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb") as file:
        file.write(pem)

    public = private.public_key()
    print(f"private key: {out}  (0600, and it does not belong anywhere else)")
    print()
    print("Put this in agent/keys/release.pub and commit it — it is what every agent")
    print("built from here will accept a release from:")
    print()
    print(f"  {_hex(public)}")
    print()
    print("And this in RELEASE_KEYS_PEM at the top of scripts/install-agent.sh, so the")
    print("first install on a host is verified on the same terms as every upgrade after it:")
    print()
    print(_public_pem(public).strip())
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    binary = Path(args.binary)
    if not binary.is_file():
        raise SystemExit(f"{binary} is not a file")

    arch = args.arch or _arch_of(binary)
    version = args.version or _version_of(binary)

    document = manifest(
        version=version,
        arch=arch,
        digest=_digest(binary),
        released_at=args.released_at or int(dt.datetime.now(tz=dt.UTC).timestamp()),
    )

    private = _load_private(Path(args.key).expanduser())
    signature = private.sign(document)

    manifest_path = binary.with_name(binary.name + ".manifest")
    signature_path = binary.with_name(binary.name + ".manifest.sig")
    manifest_path.write_bytes(document)
    signature_path.write_bytes(signature)

    print(document.decode(), end="")
    print(f"-> {manifest_path}")
    print(f"-> {signature_path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check a signed release the way an agent would, before a fleet does.

    Deliberately requires the *public* key to be named rather than reading one
    out of the manifest: a document that carries the key it is verified against
    is a document that verifies itself, which is the failure this whole design
    is arranged to avoid.
    """
    binary = Path(args.binary)
    document = binary.with_name(binary.name + ".manifest").read_bytes()
    signature = binary.with_name(binary.name + ".manifest.sig").read_bytes()

    keys = [_load_public(key) for key in args.key] or _committed_keys()
    if not keys:
        raise SystemExit(
            "no key to verify against. Pass --key, or commit one in agent/keys/ "
            "(that directory is what every built agent trusts)."
        )

    for key in keys:
        try:
            key.verify(signature, document)
            break
        except Exception:  # noqa: BLE001 - any failure is the same answer
            continue
    else:
        raise SystemExit("the manifest is not signed by any of those keys")

    fields = dict(
        line.split(" ", 1) for line in document.decode().splitlines()[1:] if " " in line
    )
    if fields.get("sha256") != _digest(binary):
        raise SystemExit(f"{binary} does not match the digest in its own manifest")

    print(f"ok: {fields.get('name')} {fields.get('version')} ({fields.get('arch')})")
    return 0


# --------------------------------------------------------------------------


def manifest(*, version: str, arch: str, digest: str, released_at: int) -> bytes:
    """The exact bytes that are signed, verified and parsed.

    Ordered and spelled the same way every time, because **the bytes are the
    contract**: the agent parses the document it was handed rather than fields
    it was told, so a signer that produced a different serialization of the
    same facts would produce a signature over something else.
    """
    return (
        f"{MAGIC}\n"
        f"name {ARTIFACT}\n"
        f"version {version}\n"
        f"arch {arch}\n"
        f"sha256 {digest}\n"
        f"released_at {released_at}\n"
    ).encode()


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _arch_of(binary: Path) -> str:
    match = _ARCH.match(binary.name)
    if match is None:
        raise SystemExit(
            f"cannot tell the architecture from {binary.name!r}; pass --arch. "
            f"`scripts/build-agent.sh` names its output {ARTIFACT}-<arch>."
        )
    return match.group("arch")


def _version_of(binary: Path) -> str:
    """Ask the binary, rather than making the operator repeat themselves.

    Only works when the release being signed is for this machine's
    architecture, which is the common case and never the assumed one: the
    failure names `--version` rather than guessing.
    """
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 - a path the caller named
            [str(binary), "--version"], capture_output=True, text=True, timeout=10
        )
    except OSError as exc:
        raise SystemExit(f"cannot run {binary} to read its version ({exc}); pass --version") from exc
    if result.returncode != 0 or not result.stdout.split():
        raise SystemExit(f"{binary} did not answer --version; pass --version")
    return result.stdout.split()[-1]


def _load_private(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} is not an ed25519 private key")
    return key


def _load_public(value: str) -> Ed25519PublicKey:
    """A key given as a file, a PEM block, or 64 hex characters."""
    path = Path(value).expanduser()
    text = path.read_text() if path.is_file() else value
    text = text.strip()
    if "PRIVATE KEY" in text:
        # Easy to do and worth naming: the two files sit beside each other and
        # only one of them is safe to pass around. `keygen` prints the public
        # half; this is the same key seen from the other side.
        raise SystemExit(
            f"{value} is a private key. The public half is what verifies: "
            f"`sign-agent.py keygen` printed it, and agent/keys/*.pub holds it."
        )
    if "BEGIN PUBLIC KEY" in text:
        key = serialization.load_pem_public_key(text.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise SystemExit("that PEM is not an ed25519 public key")
        return key
    raw = "".join(line.split("#")[0].strip() for line in text.splitlines())
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))


def _committed_keys() -> list[Ed25519PublicKey]:
    """Whatever `agent/keys/` holds, which is what a built agent trusts."""
    directory = Path(__file__).resolve().parent.parent / "agent" / "keys"
    return [_load_public(str(path)) for path in sorted(directory.glob("*.pub"))]


def _hex(key: Ed25519PublicKey) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()


def _public_pem(key: Ed25519PublicKey) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign-agent.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="Mint the key a fleet's releases are signed with")
    keygen.add_argument("--out", required=True, help="Where the private key goes")
    keygen.add_argument("--force", action="store_true", help="Overwrite an existing key")

    sign = sub.add_parser("sign", help="Write a signed manifest beside a built agent")
    sign.add_argument("binary")
    sign.add_argument("--key", required=True, help="The private key from keygen")
    sign.add_argument("--version", help="Default: ask the binary")
    sign.add_argument("--arch", help="Default: read it off the file name")
    sign.add_argument(
        "--released-at", type=int, help="Unix time in the manifest. Default: now"
    )

    verify = sub.add_parser("verify", help="Check a signed release the way an agent would")
    verify.add_argument("binary")
    verify.add_argument(
        "--key",
        action="append",
        default=[],
        help="A public key: a file, a PEM block, or hex. Default: agent/keys/*.pub",
    )

    return parser


COMMANDS = {"keygen": cmd_keygen, "sign": cmd_sign, "verify": cmd_verify}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
