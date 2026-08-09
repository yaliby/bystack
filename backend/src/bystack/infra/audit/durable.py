"""Durable audit log.

The half `infra/audit/memory.py` was missing. That one is a ring buffer a
restart erases, and a log that forgets cannot answer "who deleted the database
volume" -- which is the question ADR-0012 puts in front of every destructive
verb, and the reason none of them exist.

**"Who" is not coming, and that is a decision rather than a gap.** ADR-0014:
this is a single-operator LAN control plane with no user identity, so `actor`
is the literal string "anonymous" permanently and `CommandKind` contains no
destructive operation permanently. The two go together -- the log does not
need to name an operator for a set of verbs that cannot destroy anything.

Which leaves this file answering the half that is actually asked during an
incident: *what was attempted, against what, when, and whether it worked*,
across restarts. That value never depended on the `actor` field.

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

## The write path

Every record is still fsynced before `record` returns -- the entry this cannot
afford to lose is the one written immediately before a command that then hung
the process, and a buffered write loses exactly that one. What changed is
*where* the fsync happens: it runs in a worker thread via `asyncio.to_thread`,
because `CommandService.execute` is a coroutine and a blocking fsync there
stops every agent's pump and every browser's delta stream for its duration.
Measured at 0.01 ms on tmpfs and 0.78 ms on NVMe; on a spinning disk, a busy
host or network storage it is tens of milliseconds, per command.

Not a write-behind queue. That needs a flush on shutdown, an ordering
guarantee and a test for a crash mid-queue, and it would trade away the one
property above -- durability at the moment of return -- to buy latency nobody
is short of. Two writes per command is not a throughput problem; it was a
*latency on the wrong thread* problem, and moving the thread is the whole fix.

One lock, held across the append and any compaction it triggers, so the file
is written by one thread at a time and in the order the records were made.
Compaction is handed a snapshot taken on the event loop rather than the live
mapping, because the worker must never iterate a structure `record` can still
add to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from itertools import islice
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

    __slots__ = ("path", "_retain", "_entries", "_lines", "_writing")

    def __init__(self, path: Path, retain: int = DEFAULT_RETAIN) -> None:
        if retain < 1:
            raise ValueError("audit retention must be positive")
        self.path = path
        self._retain = retain
        #: id -> entry, oldest first. A dict rather than a deque beside an
        #: index: Python dicts are insertion-ordered, assigning an existing key
        #: keeps its position, and eviction is `next(iter(...))` -- which is
        #: every operation this class needs, each of them O(1). The deque it
        #: replaces made `finalize` a scan from the oldest end for an id that
        #: is almost always the newest.
        self._entries: dict[str, AuditEntry] = {}
        #: Records on disk, including superseded ones. The compaction trigger.
        self._lines = 0
        #: One writer at a time, and in order. See "The write path" above.
        self._writing = asyncio.Lock()

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

    async def record(self, entry: AuditEntry) -> None:
        self._remember(entry)
        await self._persist(entry)

    async def finalize(self, entry_id: str, entry: AuditEntry) -> None:
        """Append the completed form of an entry already recorded.

        A no-op for an id we no longer hold, matching the in-memory version:
        under a flood the oldest of twenty thousand concurrent operations may
        have aged out between dispatch and result, and losing its outcome is
        not worth failing the command over. The attempt is on disk either way,
        which is the half that matters during an incident.

        Assignment rather than a search: the id is already the key, and
        re-assigning an existing one keeps its insertion position, so the
        completed form lands exactly where the attempt was in the timeline.
        """
        if entry_id not in self._entries:
            return
        self._entries[entry_id] = entry
        await self._persist(entry)

    def recent(self, limit: int = 100) -> tuple[AuditEntry, ...]:
        if limit <= 0:
            return ()
        # Newest first: an operator reading an audit log is asking "what just
        # happened", never "what happened first". `reversed` over an
        # insertion-ordered dict, so answering costs the limit and not the
        # retention -- at 20,000 entries a copy-and-slice was the whole ring.
        return tuple(islice(reversed(self._entries.values()), limit))

    def __len__(self) -> int:
        return len(self._entries)

    # -- persistence -------------------------------------------------------

    def _remember(self, entry: AuditEntry) -> None:
        """Hold the entry in memory, evicting the oldest if we are full.

        Checked before the insert and only for a genuinely new id: replacing
        an entry we already hold does not grow the mapping, and evicting on
        one would drop an unrelated operation off the end of the timeline.
        """
        if entry.id not in self._entries and len(self._entries) >= self._retain:
            del self._entries[next(iter(self._entries))]
        self._entries[entry.id] = entry

    async def _persist(self, entry: AuditEntry) -> None:
        """Put one record on disk, off the event loop and in order.

        The lock is held across the append *and* the compaction it may
        trigger, so the file only ever has one writer and the records land in
        the order they were made. Compaction takes its snapshot here, on the
        loop, rather than in the worker: the worker must not iterate a mapping
        that a concurrent `record` can still add to.
        """
        async with self._writing:
            if not await asyncio.to_thread(self._append, entry):
                return
            self._lines += 1
            if self._lines > self._retain * 2:
                snapshot = tuple(self._entries.values())
                if await asyncio.to_thread(self._compact, snapshot):
                    self._lines = len(snapshot)

    def _append(self, entry: AuditEntry) -> bool:
        """One line, flushed and fsynced. Runs in a worker thread.

        Durable per record, because the entry this cannot afford to lose is
        the one written immediately before a command that then hung the
        process. Buffering would lose exactly that one -- so the fsync stays
        and only the thread it happens on changed.

        Touches nothing but the file: every counter and every mapping this
        class holds is updated by :meth:`_persist`, on the event loop. A
        worker that mutated shared state would need a lock of its own for a
        saving of one attribute assignment.

        A failed write is logged and swallowed, and reported as ``False`` so
        the caller does not count a line that is not there. `AuditLog.record`
        is documented never to fail a command by failing itself, and that is
        the right call even here: a full disk must not stop an operator
        restarting the service that filled it. The line is loud so the gap is
        findable.
        """
        try:
            handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(handle, "a") as fh:
                fh.write(json.dumps(_as_json(entry), separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.error("could not write the audit log at %s: %s", self.path, exc)
            return False
        return True

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
            # earlier one. `_remember` replaces in place and keeps the
            # original position, so the ring holds one entry per operation
            # rather than two, in the order the operations were attempted.
            self._remember(entry)

        if skipped:
            log.warning("skipped %d unreadable line(s) in %s", skipped, self.path)

    def _compact(self, entries: tuple[AuditEntry, ...]) -> bool:
        """Rewrite the file as one line per retained operation. In a worker.

        Write-and-rename, like the registry: a Controller killed during a
        compaction comes back to the previous complete file rather than to a
        truncated one. The cost is one full rewrite per ``retain`` operations,
        which is why the trigger is twice the retention and not equal to it.

        Takes the entries as an argument rather than reading them off `self`,
        because this runs off the event loop: iterating the live mapping here
        would race a `record` that is appending to it.
        """
        temporary = self.path.with_suffix(".jsonl.tmp")
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w") as fh:
                for entry in entries:
                    fh.write(json.dumps(_as_json(entry), separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("could not compact the audit log at %s: %s", self.path, exc)
            return False
        return True


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
