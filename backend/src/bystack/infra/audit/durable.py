"""Durable audit log.

The half `infra/audit/memory.py` was missing. That one is a ring buffer a
restart erases, and a log that forgets cannot answer "who deleted the database
volume" -- which is the question ADR-0012 puts in front of every destructive
verb, and the reason none of them exist.

**This file removes one of the three obstacles, and only one.** Durability is
here; RBAC is not, and `CommandKind` still contains no destructive operation.
See `docs/OPEN-WORK.md` §4 -- the three arrive together, and `actor` is
currently the literal string "anonymous" because nothing authenticates a
browser to this API at all. A durable record of "anonymous restarted the
database" is a better artifact than no record, and it is still not an answer
to "who".

## Why a file, and why this shape

The enrollment registry (`infra/agentca/registry.py`) is the precedent and
says it plainly: a table's shape, in a file, because Postgres is not in this
tree yet and moving it is then a change to one module. The same reasoning
applies here with one difference that changes the format.

The registry holds a few hundred bytes rewritten wholesale when a human
approves a host. An audit log is append-heavy -- two writes per operation, one
before dispatch and one after -- so it is **JSON Lines, appended**, never
rewritten in place. `finalize` appends a *second* record for the same id and
the later one wins on read. That is what makes a crash mid-operation leave
evidence rather than a truncated file, and it is the property the in-memory
version gets for free and a rewrite-the-whole-file design would lose.

## Retention

ADR-0006 clause 4: every table has a retention policy, and this one is by
count. The newest ``retain`` operations survive; the file is compacted when it
grows past twice that, so compaction is amortised and a busy hour never pays
for it twice. Compaction is a write-and-rename, like the registry's, so a
Controller killed during one comes back to the previous complete file rather
than to half a log.

Deliberately not by age. "Ninety days" is the policy an auditor asks for and
the wrong one to *implement* first: it makes the file's size a function of how
busy the installation is, which is exactly the unbounded growth the in-memory
version was careful to avoid. A count is a bound; a duration is a hope.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Final

from bystack.core.identity import URN
from bystack.core.ports.command import (
    AuditEntry,
    CommandKind,
    CommandStatus,
    TargetOutcome,
)

log = logging.getLogger(__name__)

_FILE = "operations.jsonl"

#: Operations retained. At ~400 bytes each this is ~8 MB on disk, which is a
#: rounding error against the disk of anything running Docker and covers
#: months of an ordinary installation rather than one session.
DEFAULT_RETAIN: Final = 20_000


class DurableAuditLog:
    """Append-only :class:`~bystack.core.ports.command.AuditLog`, on disk.

    Reads are served from memory: the newest ``retain`` entries are held in a
    ring exactly as the in-memory implementation holds them, and the file is
    the copy that survives a restart. `recent` never touches the disk, so the
    timeline the UI polls costs the same as it did before.
    """

    __slots__ = ("path", "_retain", "_entries", "_index", "_lines")

    def __init__(self, path: Path, retain: int = DEFAULT_RETAIN) -> None:
        if retain < 1:
            raise ValueError("audit retention must be positive")
        self.path = path
        self._retain = retain
        self._entries: deque[AuditEntry] = deque(maxlen=retain)
        self._index: dict[str, AuditEntry] = {}
        #: Records on disk, including superseded ones. The compaction trigger.
        self._lines = 0

    @classmethod
    def open(
        cls, directory: str | os.PathLike[str], retain: int = DEFAULT_RETAIN
    ) -> DurableAuditLog:
        """Load what a previous process wrote, and be ready to append.

        The directory is created 0700 rather than assumed: this is the first
        thing to touch it on an installation that has never enrolled a host,
        and an audit log the group can read is not one.
        """
        base = Path(directory).expanduser()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        audit = cls(base / _FILE, retain)
        audit._load()
        return audit

    # -- AuditLog ----------------------------------------------------------

    def record(self, entry: AuditEntry) -> None:
        self._remember(entry)
        self._append(entry)

    def finalize(self, entry_id: str, entry: AuditEntry) -> None:
        """Append the completed form of an entry already recorded.

        A no-op for an id we no longer hold, matching the in-memory version:
        under a flood the oldest of twenty thousand concurrent operations may
        have aged out between dispatch and result, and losing its outcome is
        not worth failing the command over. The attempt is on disk either way,
        which is the half that matters during an incident.

        **KNOWN GAP — `docs/OPEN-WORK.md` §3.5.** The scan below starts at the
        oldest end to find an id that is almost always the newest, so it is
        O(retain): 0.50 ms with the ring full. Inherited from `memory.py`,
        where the ring is 2,000 and it did not matter; raising retention to
        20,000 made it 10x worse without anyone deciding to.
        """
        if entry_id not in self._index:
            return
        for position, existing in enumerate(self._entries):
            if existing.id == entry_id:
                self._entries[position] = entry
                self._index[entry_id] = entry
                self._append(entry)
                return

    def recent(self, limit: int = 100) -> tuple[AuditEntry, ...]:
        if limit <= 0:
            return ()
        # Newest first: an operator reading an audit log is asking "what just
        # happened", never "what happened first".
        return tuple(self._entries)[-limit:][::-1]

    def __len__(self) -> int:
        return len(self._entries)

    # -- persistence -------------------------------------------------------

    def _remember(self, entry: AuditEntry) -> None:
        if len(self._entries) == self._entries.maxlen:
            self._index.pop(self._entries[0].id, None)
        self._entries.append(entry)
        self._index[entry.id] = entry

    def _append(self, entry: AuditEntry) -> None:
        """One line, flushed and fsynced.

        Synchronous and durable per record, because the entry this cannot
        afford to lose is the one written immediately before a command that
        then hung the process. Buffering would lose exactly that one.

        **KNOWN GAP — `docs/OPEN-WORK.md` §3.1. Fix this before adding
        anything here.** This runs on the event loop: `CommandService.execute`
        is a coroutine and calls `record` and `finalize` directly, so every
        command stops the agent pumps and the browser streams for the length
        of two fsyncs. Measured 0.01 ms on tmpfs and 0.78 ms on NVMe, which
        means the test suite cannot see it -- `tmp_path` is tmpfs. On slower
        storage it is tens of milliseconds. The fix is `asyncio.to_thread`,
        not a write-behind queue; the doc says why.

        A failed write is logged and swallowed. `AuditLog.record` is
        documented never to fail a command by failing itself, and that is the
        right call even here: a full disk must not stop an operator restarting
        the service that filled it. The line is loud so the gap is findable.
        """
        try:
            handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(handle, "a") as fh:
                fh.write(json.dumps(_as_json(entry), separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.error("could not write the audit log at %s: %s", self.path, exc)
            return

        self._lines += 1
        if self._lines > self._retain * 2:
            self._compact()

    def _load(self) -> None:
        """Read the file back into the ring, newest wins.

        A malformed line is skipped rather than fatal, which is the opposite
        of the enrollment registry's rule and deliberately so. An unreadable
        allow-list means we do not know who is approved and either answer is
        an outage; an unreadable audit line means we have lost one record of
        the past, and refusing to start over it would turn a corrupted log
        into a control plane that will not run.
        """
        if not self.path.exists():
            return

        skipped = 0
        try:
            raw_lines = self.path.read_text().splitlines()
        except OSError as exc:
            log.error("could not read the audit log at %s: %s", self.path, exc)
            return

        for line in raw_lines:
            if not line.strip():
                continue
            self._lines += 1
            try:
                entry = _from_json(json.loads(line))
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            # A later record for the same id is the finalized form of an
            # earlier one. Replace in place so the ring holds one entry per
            # operation rather than two.
            if entry.id in self._index:
                for position, existing in enumerate(self._entries):
                    if existing.id == entry.id:
                        self._entries[position] = entry
                        break
                self._index[entry.id] = entry
            else:
                self._remember(entry)

        if skipped:
            log.warning("skipped %d unreadable line(s) in %s", skipped, self.path)

    def _compact(self) -> None:
        """Rewrite the file as one line per retained operation.

        Write-and-rename, like the registry: a Controller killed during a
        compaction comes back to the previous complete file rather than to a
        truncated one. The cost is one full rewrite per ``retain`` operations,
        which is why the trigger is twice the retention and not equal to it.
        """
        temporary = self.path.with_suffix(".jsonl.tmp")
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w") as fh:
                for entry in self._entries:
                    fh.write(json.dumps(_as_json(entry), separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("could not compact the audit log at %s: %s", self.path, exc)
            return
        self._lines = len(self._entries)


# --------------------------------------------------------------------------
# The record shape.
#
# Explicit both ways rather than `dataclasses.asdict` and a constructor
# splat. A field added to `AuditEntry` should be a deliberate change to the
# on-disk format -- that is what "the shape a table would have" means -- and
# an automatic mapping would silently start writing it and silently fail to
# read a file written by the version before.
# --------------------------------------------------------------------------


def _as_json(entry: AuditEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "at": entry.at,
        "actor": entry.actor,
        "kind": str(entry.kind),
        "target": str(entry.target),
        "targets": [str(urn) for urn in entry.targets],
        "status": str(entry.status),
        "detail": entry.detail,
        "reason": entry.reason,
        "duration_ms": entry.duration_ms,
        "outcomes": [
            {"target": str(o.target), "status": str(o.status), "detail": o.detail}
            for o in entry.outcomes
        ],
    }


def _from_json(raw: dict[str, Any]) -> AuditEntry:
    return AuditEntry(
        id=str(raw["id"]),
        at=float(raw["at"]),
        actor=str(raw.get("actor", "anonymous")),
        kind=CommandKind(str(raw["kind"])),
        target=URN(str(raw["target"])),
        targets=tuple(URN(str(urn)) for urn in raw.get("targets", ())),
        status=CommandStatus(str(raw.get("status", CommandStatus.IN_FLIGHT))),
        detail=raw.get("detail"),
        reason=raw.get("reason"),
        duration_ms=int(raw.get("duration_ms", 0)),
        outcomes=tuple(
            TargetOutcome(
                URN(str(o["target"])), CommandStatus(str(o["status"])), o.get("detail")
            )
            for o in raw.get("outcomes", ())
        ),
    )
