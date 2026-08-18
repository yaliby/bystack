"""Agent releases the Controller can hand out.

A directory of artifacts, each with the signed manifest that makes it worth
installing. The Controller reads them, indexes them by architecture, and sends
them; it holds no signing key and cannot make one of these files (ADR-0017).

    /var/lib/bystack/releases/
        bystack-agent-x86_64
        bystack-agent-x86_64.manifest
        bystack-agent-x86_64.manifest.sig
        bystack-agent-aarch64
        bystack-agent-aarch64.manifest
        bystack-agent-aarch64.manifest.sig

**How the bytes got there is not a trust decision, and that is the point.**
Before ADR-0017 the objection to a Controller-driven upgrade was that a
Controller upgrading a mixed fleet must fetch artifacts for architectures it is
not -- moving an internet download onto the machine that already faces the
internet. With verification at the agent, provenance no longer depends on how
the Controller obtained the bytes: fetching from GitHub Releases, shipping
inside the wheel, or an operator dropping files in this directory are
equivalent, and the choice is an operational convenience.

So this module *scans a directory*, and nothing here downloads anything.

Two things it does check, and neither is a security control: that the manifest
parses under the same strict rules the agent applies, and that the digest
inside it matches the file beside it. Both are there so an operator who paired
the wrong files finds out here -- with a log line naming the file -- rather
than on the fleet, one host at a time, as a refusal.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The first line of a manifest. An agent refuses one whose magic it does not
#: know, so the Controller indexing it would be indexing something no host will
#: install; mirrored here for that reason and checked in `test_upgrade.py`
#: against the Rust constant.
MAGIC = "bystack-manifest/1"

#: What is being distributed. The name is inside the signed document because a
#: key that ever signs a second artifact must not let one be presented as the
#: other, and it is checked here so a manifest for something else is not
#: offered to agents as an agent.
ARTIFACT = "bystack-agent"

_REQUIRED = frozenset({"name", "version", "arch", "sha256", "released_at"})


class ReleaseError(ValueError):
    """A file in the release directory that cannot be handed to a fleet."""


@dataclass(frozen=True, slots=True)
class Release:
    """One artifact, and the signed document that vouches for it."""

    version: str
    arch: str
    sha256: str
    released_at: int
    binary: Path
    manifest: bytes
    """The signed document, **verbatim**.

    Kept as bytes and never rebuilt from the fields above. What the agent
    verifies is the byte string that was signed, so a Controller that
    re-serialized it from parsed fields would be sending a document whose
    signature covers something else -- and every host in the fleet would refuse
    it with a message about trust.
    """

    signature: bytes
    size: int

    @property
    def name(self) -> str:
        return ARTIFACT

    def read(self) -> bytes:
        """The artifact itself. Read on demand, not held in the index.

        A Controller holding every architecture's binary in memory forever
        would be paying the fleet's distribution cost at idle; two megabytes
        during a rollout is a different thing from two megabytes always.
        """
        return self.binary.read_bytes()


class ReleaseStore:
    """The release directory, re-read when asked.

    Not watched and not cached across calls. Scanning is a handful of stats
    and, for anything that changed, one hash of a two-megabyte file -- and the
    alternative is an index that disagrees with the directory an operator just
    copied a file into, which is exactly the moment they are looking at the
    screen.
    """

    __slots__ = ("_directory", "_digests")

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory).expanduser()
        #: (path, mtime, size) -> digest. The one thing worth remembering
        #: between scans: rehashing an unchanged file on every poll of the
        #: releases route is the only part of this that is not free.
        self._digests: dict[tuple[str, int, int], str] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    def all(self) -> list[Release]:
        """Every usable release, newest first.

        A file that cannot be used is logged and skipped rather than raised:
        one mispaired manifest must not make the other architecture's release
        unavailable, and the operator needs the sentence naming which file it
        was.
        """
        if not self._directory.is_dir():
            return []

        found: list[Release] = []
        for manifest in sorted(self._directory.glob("*.manifest")):
            try:
                found.append(self._load(manifest))
            except (ReleaseError, OSError) as exc:
                log.warning("ignoring %s: %s", manifest.name, exc)
        found.sort(key=lambda release: (release.released_at, release.version), reverse=True)
        return found

    def latest(self, arch: str) -> Release | None:
        """The newest release for one architecture, or ``None``.

        By `released_at` rather than by comparing version strings. The
        Controller has no stake in what `0.4.0-rc1` is relative to `0.4.0` --
        the agent decides that, against the binary on its own disk, and it is
        the only side whose answer is authoritative (ADR-0017). What this needs
        is a deterministic order for choosing what to offer.
        """
        for release in self.all():
            if release.arch == arch:
                return release
        return None

    def find(self, version: str, arch: str) -> Release | None:
        for release in self.all():
            if release.version == version and release.arch == arch:
                return release
        return None

    def versions(self) -> list[str]:
        """Distinct versions held, newest first. What a rollout may target."""
        seen: dict[str, None] = {}
        for release in self.all():
            seen[release.version] = None
        return list(seen)

    # -- internals ---------------------------------------------------------

    def _load(self, manifest_path: Path) -> Release:
        binary = manifest_path.with_suffix("")
        signature_path = manifest_path.with_name(manifest_path.name + ".sig")

        if not binary.is_file():
            raise ReleaseError(f"there is no {binary.name} beside it")
        if not signature_path.is_file():
            raise ReleaseError(
                f"there is no {signature_path.name}; an unsigned artifact is one no "
                f"agent will install"
            )

        document = manifest_path.read_bytes()
        fields = parse_manifest(document)
        digest = self._digest(binary)
        if digest != fields["sha256"]:
            raise ReleaseError(
                f"{binary.name} does not match the digest in its manifest "
                f"(the manifest says {fields['sha256'][:12]}…, the file is {digest[:12]}…)"
            )

        return Release(
            version=fields["version"],
            arch=fields["arch"],
            sha256=digest,
            released_at=int(fields["released_at"]),
            binary=binary,
            manifest=document,
            signature=signature_path.read_bytes(),
            size=binary.stat().st_size,
        )

    def _digest(self, path: Path) -> str:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._digests.get(key)
        if cached is not None:
            return cached

        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        # Bounded by the number of artifacts in one directory, which is one per
        # architecture per version an operator chose to keep.
        self._digests[key] = digest.hexdigest()
        return self._digests[key]


def parse_manifest(document: bytes) -> dict[str, str]:
    """The same strict read the agent performs, on the Controller's side.

    Strict on purpose, and symmetric with `agent/src/upgrade.rs` rather than
    merely similar: a manifest this accepted and an agent refused would be a
    release the dashboard offers and every host turns down, which is the worst
    place to find out. Unknown keys and an unknown magic are errors here for
    the same reason they are errors there.
    """
    try:
        text = document.decode()
    except UnicodeDecodeError as exc:
        raise ReleaseError("the manifest is not UTF-8") from exc

    lines = text.splitlines()
    if not lines or lines[0].strip() != MAGIC:
        raise ReleaseError(
            f"unknown manifest format {lines[0].strip()!r} if any; this Controller reads {MAGIC}"
        )

    fields: dict[str, str] = {}
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        key, separator, value = line.partition(" ")
        if not separator:
            raise ReleaseError(f"manifest line {line!r} is not `key value`")
        if key not in _REQUIRED:
            raise ReleaseError(f"unknown manifest key {key!r}")
        if key in fields:
            raise ReleaseError(f"the manifest names {key!r} twice")
        fields[key] = value.strip()

    missing = _REQUIRED - fields.keys()
    if missing:
        raise ReleaseError(f"the manifest has no {', '.join(sorted(missing))}")
    if fields["name"] != ARTIFACT:
        raise ReleaseError(f"this is a signed {fields['name']!r}, not a {ARTIFACT}")
    if not fields["released_at"].isdigit():
        raise ReleaseError(f"released_at {fields['released_at']!r} is not a unix time")
    if len(fields["sha256"]) != 64:
        raise ReleaseError("the manifest's sha256 is not a SHA-256")
    return fields
