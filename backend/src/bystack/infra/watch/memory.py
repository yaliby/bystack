"""Watch lists in memory.

The whole store, minus the file. `durable.py` wraps this rather than
reimplementing it, because unlike the audit log — where the durable version
needed a different *format* to survive a crash mid-operation — a watch list is
the same small mapping either way, and the only thing that changes is whether
it outlives the process.

Used on its own by the test suite and by a Controller with nowhere to write,
where it is the wrong answer offered honestly: the selection is lost on
restart, which is visible immediately, rather than a start-up refusal that
takes the whole control plane down over a preference.
"""

from __future__ import annotations

import time
import uuid

from bystack.core.identity import engine_scope
from bystack.core.ports.watch import (
    MAX_ENTRIES,
    WatchEntry,
    WatchRejected,
    normalize,
)


class InMemoryWatchStore:
    """A :class:`~bystack.core.ports.watch.WatchStore` that forgets on restart."""

    __slots__ = ("_by_host",)

    def __init__(self, entries: tuple[WatchEntry, ...] = ()) -> None:
        # engine id -> entry id -> entry, insertion-ordered, which is the order
        # an operator added them and therefore the order they should read in.
        self._by_host: dict[str, dict[str, WatchEntry]] = {}
        for entry in entries:
            self._by_host.setdefault(entry.engine_id, {})[entry.id] = entry

    # -- reads -------------------------------------------------------------

    def entries(self, engine_id: str) -> tuple[WatchEntry, ...]:
        return tuple(self._by_host.get(engine_scope(engine_id), {}).values())

    def all(self) -> tuple[WatchEntry, ...]:
        return tuple(
            entry for host in self._by_host.values() for entry in host.values()
        )

    def get(self, engine_id: str, entry_id: str) -> WatchEntry | None:
        return self._by_host.get(engine_scope(engine_id), {}).get(entry_id)

    def __len__(self) -> int:
        return sum(len(host) for host in self._by_host.values())

    # -- writes ------------------------------------------------------------

    async def add(self, entry: WatchEntry) -> WatchEntry:
        stored = self._prepare(entry)
        self._by_host.setdefault(stored.engine_id, {})[stored.id] = stored
        return stored

    async def remove(self, engine_id: str, entry_id: str) -> bool:
        key = engine_scope(engine_id)
        host = self._by_host.get(key)
        if host is None or entry_id not in host:
            return False
        del host[entry_id]
        if not host:
            # A host with nothing selected holds no mapping, so an installation
            # that watched something once and stopped is indistinguishable from
            # one that never did — which is true, and keeps the store from
            # growing an entry per host ever seen.
            del self._by_host[key]
        return True

    # -- internals ---------------------------------------------------------

    def _prepare(self, entry: WatchEntry) -> WatchEntry:
        """Normalize, bound, and assign the parts the caller left blank.

        The id and the timestamp are assigned *here* rather than by the route,
        so that every entry point produces the same shape and no client ever
        gets to choose an id — a client-chosen id is a client that can
        overwrite somebody else's entry by guessing, and it is free not to
        allow.

        The group id is the one thing a caller may bring, and only because a
        fan-out has to: nine entries on nine hosts are nine calls to this
        method, and the whole point is that they share a group. The rule the
        paragraph above is really making still holds — the *client* cannot
        choose one, because `WatchEntryIn` has no such field and the fan-out
        route mints the value itself. A blank one becomes a fresh group of one,
        which is what a plain per-host add is.
        """
        engine_id = engine_scope(entry.engine_id)
        if not engine_id:
            raise WatchRejected("a watch entry needs a host")

        stored = normalize(
            WatchEntry(
                id=entry.id or uuid.uuid4().hex[:12],
                engine_id=engine_id,
                kind=entry.kind,
                group_id=entry.group_id or uuid.uuid4().hex[:12],
                name=entry.name,
                match_kind=entry.match_kind,
                pattern=entry.pattern,
                label=entry.label,
                added_at=entry.added_at or time.time(),
            )
        )

        host = self._by_host.get(engine_id, {})
        # Replacing an entry we already hold is an edit and does not grow the
        # list, so the ceiling is checked only for a genuinely new id.
        if stored.id not in host and len(host) >= MAX_ENTRIES:
            raise WatchRejected(
                f"this host already watches {MAX_ENTRIES} things, which is the limit; "
                f"remove one before adding another"
            )
        duplicate = next(
            (
                existing
                for existing in host.values()
                if existing.id != stored.id
                and existing.kind is stored.kind
                and existing.target == stored.target
                and existing.match_kind == stored.match_kind
            ),
            None,
        )
        if duplicate is not None:
            # Refused rather than silently deduplicated: two entries for the
            # same target would be two nodes with the same content and
            # different URNs, drawn twice on the map, and the operator who
            # added the second one is entitled to know why it did not appear.
            raise WatchRejected(f"this host already watches {stored.target!r}")
        return stored
