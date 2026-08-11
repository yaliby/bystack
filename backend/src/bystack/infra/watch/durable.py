"""Watch lists on disk.

ADR-0001 permits durable storage for exactly four categories, and this is the
first one: **configuration — user intent, not discoverable.** The graph is
rebuilt from the fleet within seconds of a cold start and this cannot be,
because there is nothing out there to ask. A Controller that lost this file
would come back having forgotten which services the operator selected, and the
only symptom would be a map missing things nobody could explain the absence of.

## Why the whole file, rewritten

The opposite of the audit log's format, on purpose. That one is append-heavy —
two records per operation — so it is JSON Lines and a crash mid-write costs one
line. This is a few dozen entries changed by hand at human speed, and the
thing it must survive is a Controller killed between two of them. A
write-and-rename of the complete list gives that for one small write per edit;
an append-only log of additions and removals would have to be replayed, and
replay of a log with a delete in it is the part that goes wrong.

Same rules as the audit log otherwise, and for the same reasons: 0600 in a
0700 directory, fsynced before the write returns, and **the file work happens
off the event loop** (ADR-0012 section 4b). The loop this would otherwise block
is the one driving every agent's pump.

## Retention

Clause 4 asks every durable store for a policy, and configuration's is a
ceiling rather than a TTL: :data:`~bystack.core.ports.watch.MAX_ENTRIES` per
host, enforced on the way in. An age-based rule would be actively wrong here —
a watch on a service that has been running untouched for a year is the *most*
settled entry in the file, not the stalest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from bystack.core.ports.watch import MatchKind, WatchEntry, WatchKind
from bystack.infra.watch.memory import InMemoryWatchStore

log = logging.getLogger(__name__)

_FILE = "watchlist.json"


class DurableWatchStore:
    """A :class:`~bystack.core.ports.watch.WatchStore` that survives a restart.

    Composes the in-memory store rather than inheriting from it: everything
    about *what a watch list is* — the ceiling, the duplicate rule, the id
    assignment — belongs to one implementation, and this class is only the
    question of where it lives between two runs. Reads never touch the disk.
    """

    __slots__ = ("path", "_memory", "_writing")

    def __init__(self, path: Path, entries: tuple[WatchEntry, ...] = ()) -> None:
        self.path = path
        self._memory = InMemoryWatchStore(entries)
        #: One writer at a time, so two edits arriving together cannot
        #: interleave their renames and leave the older list on disk.
        self._writing = asyncio.Lock()

    @classmethod
    def open(cls, directory: str | os.PathLike[str]) -> DurableWatchStore:
        """Load what a previous process wrote, and be ready to save."""
        base = Path(directory).expanduser()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cls(base / _FILE, _load(base / _FILE))

    # -- reads -------------------------------------------------------------

    def entries(self, engine_id: str) -> tuple[WatchEntry, ...]:
        return self._memory.entries(engine_id)

    def all(self) -> tuple[WatchEntry, ...]:
        return self._memory.all()

    def get(self, engine_id: str, entry_id: str) -> WatchEntry | None:
        return self._memory.get(engine_id, entry_id)

    def __len__(self) -> int:
        return len(self._memory)

    # -- writes ------------------------------------------------------------

    async def add(self, entry: WatchEntry) -> WatchEntry:
        stored = await self._memory.add(entry)
        await self._save()
        return stored

    async def remove(self, engine_id: str, entry_id: str) -> bool:
        removed = await self._memory.remove(engine_id, entry_id)
        if removed:
            await self._save()
        return removed

    # -- persistence -------------------------------------------------------

    async def _save(self) -> None:
        """Write the whole list, off the event loop and one writer at a time.

        The snapshot is taken here, on the loop, and handed to the worker: a
        thread iterating the live mapping would race an edit arriving on the
        next request.
        """
        async with self._writing:
            snapshot = self._memory.all()
            await asyncio.to_thread(self._write, snapshot)

    def _write(self, entries: tuple[WatchEntry, ...]) -> None:
        """Write-and-rename, fsynced. Runs in a worker thread.

        A failure is logged and swallowed rather than raised. The alternative
        is failing the operator's edit *after* it has taken effect in memory,
        which would leave the UI, the fleet and the disk each holding a
        different answer; this way the selection works for as long as the
        process lives and the reason it will not survive a restart is in the
        log, loudly, where a full disk belongs.
        """
        temporary = self.path.with_suffix(".json.tmp")
        payload = {"entries": [_as_json(entry) for entry in entries]}
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w") as fh:
                json.dump(payload, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("could not write the watch list at %s: %s", self.path, exc)


# --------------------------------------------------------------------------
# The record shape. Explicit both ways, exactly as the audit log's is: a field
# added to `WatchEntry` should be a deliberate change to the on-disk format,
# and an automatic mapping would silently start writing it and silently fail
# to read a file written by the version before.
# --------------------------------------------------------------------------


def _as_json(entry: WatchEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "engine_id": entry.engine_id,
        "kind": str(entry.kind),
        "name": entry.name,
        "match_kind": str(entry.match_kind) if entry.match_kind else None,
        "pattern": entry.pattern,
        "label": entry.label,
        "added_at": entry.added_at,
    }


def _from_json(raw: dict[str, Any]) -> WatchEntry:
    match_kind = raw.get("match_kind")
    return WatchEntry(
        id=str(raw["id"]),
        engine_id=str(raw["engine_id"]),
        kind=WatchKind(str(raw["kind"])),
        name=str(raw.get("name", "")),
        match_kind=MatchKind(str(match_kind)) if match_kind else None,
        pattern=str(raw.get("pattern", "")),
        label=str(raw.get("label", "")),
        added_at=float(raw.get("added_at", 0.0)),
    )


def _load(path: Path) -> tuple[WatchEntry, ...]:
    """Read the file back, skipping anything unreadable.

    An entry that cannot be parsed is dropped rather than fatal — the same call
    the audit log makes and the opposite of the enrollment registry's. A
    corrupted allow-list means we do not know who is approved and either answer
    is an outage; a corrupted watch entry means one card is missing from a map,
    and refusing to start over it would turn that into a control plane that
    will not run.
    """
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.error("could not read the watch list at %s: %s", path, exc)
        return ()

    entries: list[WatchEntry] = []
    skipped = 0
    for record in raw.get("entries", ()) if isinstance(raw, dict) else ():
        try:
            entries.append(_from_json(record))
        except (ValueError, KeyError, TypeError):
            skipped += 1
    if skipped:
        log.warning("skipped %d unreadable watch entr(ies) in %s", skipped, path)
    return tuple(entries)
