"""The two files the Controller and `bystack-manager` talk through (ADR-0018).

ADR-0017 gave the fleet a button: the Controller distributes a signed release
and each host verifies it against a key the Controller does not have. It left
one machine out, necessarily -- **the Controller has no parent to push to it.**

ADR-0018 closes that with a local root component, `bystack-manager`, that pulls
from GitHub Releases and installs under the same signature contract. This
module is the Controller's end of the conversation, and it is deliberately the
smaller end:

    /opt/bystack/ipc/update.intent    this process writes it. Root reads it.
    /opt/bystack/ipc/update.status    root writes it. This process reads it.

**Nothing here is trusted and nothing here is an instruction.** The intent
carries a version, which the manager checks for being a version and then uses
to compose a URL under a repository compiled into itself; what comes back is
refused unless it is signed by a key compiled into itself. So the worst this
file can do -- including from a Controller in an attacker's hands -- is cause
its own host to install a genuine, current, correctly signed Controller. That
is the property ADR-0017 gives the Controller over the fleet, pointed the other
way.

The status file is the one thing in the feature the Controller cannot say for
itself: `ipc/` is `root:bystack 1770`, so this process can create its own
intent and cannot unlink root's report.

Files rather than a socket, and rather than a REST call into a root daemon. A
socket needs a listener, and the listener would have to be the root process --
a resident root daemon on the box for the 99.99% of its life when nobody is
updating anything. What is actually communicated is one sentence a few times a
year.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The first line of each document. Bumped only by a release that teaches both
#: ends to read the new one -- the same discipline the signed manifest has, and
#: for the same reason: a field added later because it carries a constraint is
#: a field the other end must not quietly ignore.
#:
#: Mirrored from `agent/manager/src/ipc.rs` and checked against it by
#: `test_manager.py`, the way `test_wire.py` checks the manifest magic. Two
#: languages, one document, and nothing in either build derives one from the
#: other.
INTENT_MAGIC = "bystack-intent/1"
STATUS_MAGIC = "bystack-status/1"

INTENT_NAME = "update.intent"
STATUS_NAME = "update.status"

#: Where a packaged install puts them.
DEFAULT_IPC_DIR = "/opt/bystack/ipc"

#: A run that reached one of these is over. Anything else is in flight, and the
#: dashboard keeps polling.
TERMINAL = frozenset({"success", "failed", "rolled_back"})

#: Neither document is ever more than a few hundred bytes. A cap rather than a
#: `read()`, because this path is polled and the file is written by another
#: process.
_MAX = 4 * 1024

_STATUS_KEYS = frozenset({"phase", "version", "cascade", "updated_at", "detail"})


class ManagerError(RuntimeError):
    """Something the operator needs a sentence about, not a traceback."""


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """Where the manager has got to, as it last reported."""

    phase: str
    version: str
    detail: str
    cascade: str
    updated_at: int

    @property
    def running(self) -> bool:
        return self.phase not in TERMINAL

    @property
    def failed(self) -> bool:
        return self.phase in ("failed", "rolled_back")


class ManagerLink:
    """The `ipc/` directory, read and written on demand.

    Not watched and not cached. The status file is written by another process
    a handful of times per update, and the cost of being right about it is one
    `stat` and a few hundred bytes -- against a cached answer that disagrees
    with the file at exactly the moment somebody is watching a progress bar.
    """

    __slots__ = ("_directory",)

    def __init__(self, directory: str | Path = DEFAULT_IPC_DIR) -> None:
        self._directory = Path(directory).expanduser()

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def installed(self) -> bool:
        """Whether this machine has a manager to talk to.

        A checkout, a container and a `pip install` into a venv all answer
        `False`, and that is the honest answer rather than a degraded one:
        there is no single file for anything to rename, so the dashboard offers
        the documented upgrade path instead of a button that would write a file
        nothing reads.
        """
        return self._directory.is_dir() and os.access(self._directory, os.W_OK)

    def request(self, version: str, health: str) -> None:
        """Ask for an update. The whole of the Controller's part in one write.

        Written whole and then renamed. The manager's path unit fires on the
        file existing, so a partial write is a trigger for a document that is
        not finished -- and the manager, reading strictly, would refuse it and
        report the update as broken before it began.
        """
        if not self.installed:
            raise ManagerError(
                f"there is no {self._directory} to write to, so this Controller has no "
                f"local updater. That is what a container, a checkout or a `pip install` "
                f"looks like; upgrade it the way it was installed."
            )
        document = (
            f"{INTENT_MAGIC}\n"
            f"version {version}\n"
            f"health {health}\n"
            f"requested_at {int(time.time())}\n"
        )
        target = self._directory / INTENT_NAME
        temporary = self._directory / f".{INTENT_NAME}.new"
        try:
            temporary.write_text(document)
            temporary.replace(target)
        except OSError as exc:
            raise ManagerError(f"cannot ask for an update: {exc}") from exc

    def status(self) -> UpdateStatus | None:
        """What the last run reported, or `None` if there has never been one.

        A document this cannot parse is `None` with a log line rather than an
        exception. It is polled from a route that a browser is holding open,
        and a Controller that started returning 500s because root wrote
        something unexpected would be a worse failure than the one being
        reported.
        """
        path = self._directory / STATUS_NAME
        try:
            if path.stat().st_size > _MAX:
                log.warning("%s is too large to be a status document", path)
                return None
            document = path.read_text()
        except OSError:
            return None

        try:
            fields = _parse(document, STATUS_MAGIC, _STATUS_KEYS)
        except ValueError as exc:
            log.warning("ignoring %s: %s", path, exc)
            return None

        return UpdateStatus(
            phase=fields.get("phase", ""),
            version=fields.get("version", ""),
            detail=fields.get("detail", ""),
            cascade=fields.get("cascade", ""),
            updated_at=int(fields.get("updated_at") or 0),
        )


def _parse(document: str, magic: str, known: frozenset[str]) -> dict[str, str]:
    """The same strict read the Rust end performs, on this side.

    Strict on purpose and symmetric with `agent/manager/src/ipc.rs` rather than
    merely similar: a document one accepted and the other refused would be an
    update that reports differently depending on which end you ask.
    """
    lines = document.splitlines()
    if not lines or lines[0].strip() != magic:
        first = lines[0].strip() if lines else ""
        raise ValueError(f"unknown format {first!r}; this reads {magic}")

    fields: dict[str, str] = {}
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key not in known:
            raise ValueError(f"unknown key {key!r}")
        if key in fields:
            raise ValueError(f"the document names {key!r} twice")
        fields[key] = value.strip()
    return fields
