"""In-memory audit log.

Bounded by construction. ADR-0006 clause 4 -- *every table has a retention
policy* -- applies to the in-memory form too, and more urgently: an unbounded
list of operations is a memory leak that only manifests on the busiest
installation, which is the one that can least afford it.

**No longer the default.** `durable.py` is, and this is now the fallback for a
Controller that has nowhere to write -- a read-only root, a container with no
volume, or an operator who set `audit.durable: false`. It stays because
"cannot persist" and "will not start" are different answers and only one of
them is acceptable for a control plane.

Kept deliberately simple for that reason: it is the implementation that has to
work when the interesting one cannot. The retention semantics are identical,
so a Controller that falls back here answers `GET /commands/audit` in the same
shape and the same order, and differs only in how far back it can see.
"""

from __future__ import annotations

from itertools import islice
from typing import Final

from bystack.core.ports.command import AuditEntry

#: Entries retained. At ~400 bytes each this is well under 1 MB, and it
#: comfortably covers the window an operator actually reviews -- the last
#: session, not the last quarter. Reviewing the last quarter is what the
#: durable log will be for.
DEFAULT_CAPACITY: Final = 2000


class InMemoryAuditLog:
    """Bounded, newest-first :class:`~bystack.core.ports.command.AuditLog`.

    One insertion-ordered ``dict`` keyed by operation id, which is a ring, an
    index and an eviction policy at once: assigning an existing key keeps its
    position, so `finalize` is an assignment, and the oldest entry is
    ``next(iter(...))``. The deque-plus-side-index this replaces had to keep
    the two in step by hand, and made `finalize` scan from the oldest end for
    an id that is almost always the newest.
    """

    __slots__ = ("_capacity", "_entries")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("audit capacity must be positive")
        self._capacity = capacity
        self._entries: dict[str, AuditEntry] = {}

    async def record(self, entry: AuditEntry) -> None:
        """Nothing to await, and a coroutine anyway.

        The signature belongs to the port, not to this implementation: a
        `record` whose sync-ness depended on which log the composition root
        chose would put that choice into the shape of every caller. See
        `core/ports/command.AuditLog`.
        """
        self._evict_if_full(entry)
        self._entries[entry.id] = entry

    async def finalize(self, entry_id: str, entry: AuditEntry) -> None:
        """Replace an in-flight entry with its completed form.

        A no-op if the entry has already been evicted -- which is possible
        under a flood of commands, and is not worth failing the command over.
        The attempt was recorded; losing the outcome of the oldest of two
        thousand concurrent operations is an acceptable trade against
        unbounded growth.
        """
        if entry_id not in self._entries:
            return
        self._entries[entry_id] = entry

    def recent(self, limit: int = 100) -> tuple[AuditEntry, ...]:
        if limit <= 0:
            return ()
        # Newest first: an operator reading an audit log is asking "what just
        # happened", never "what happened first".
        return tuple(islice(reversed(self._entries.values()), limit))

    def __len__(self) -> int:
        return len(self._entries)

    def _evict_if_full(self, entry: AuditEntry) -> None:
        """Drop the oldest, and only for an id that is genuinely new.

        Re-recording an id we already hold does not grow the mapping, so
        evicting on one would drop an unrelated operation off the end of the
        timeline for nothing.
        """
        if entry.id not in self._entries and len(self._entries) >= self._capacity:
            del self._entries[next(iter(self._entries))]
