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

from collections import deque
from typing import Final

from bystack.core.ports.command import AuditEntry

#: Entries retained. At ~400 bytes each this is well under 1 MB, and it
#: comfortably covers the window an operator actually reviews -- the last
#: session, not the last quarter. Reviewing the last quarter is what the
#: durable log will be for.
DEFAULT_CAPACITY: Final = 2000


class InMemoryAuditLog:
    """Bounded, newest-first :class:`~bystack.core.ports.command.AuditLog`."""

    __slots__ = ("_entries", "_index")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("audit capacity must be positive")
        self._entries: deque[AuditEntry] = deque(maxlen=capacity)
        # id -> entry, so `finalize` does not scan. Kept in step with the
        # deque's own eviction below rather than allowed to grow beside it --
        # a side index that outlives its ring is the leak this class exists
        # to avoid.
        self._index: dict[str, AuditEntry] = {}

    def record(self, entry: AuditEntry) -> None:
        if len(self._entries) == self._entries.maxlen:
            evicted = self._entries[0]
            self._index.pop(evicted.id, None)
        self._entries.append(entry)
        self._index[entry.id] = entry

    def finalize(self, entry_id: str, entry: AuditEntry) -> None:
        """Replace an in-flight entry with its completed form.

        A no-op if the entry has already been evicted -- which is possible
        under a flood of commands, and is not worth failing the command over.
        The attempt was recorded; losing the outcome of the oldest of two
        thousand concurrent operations is an acceptable trade against
        unbounded growth.
        """
        if entry_id not in self._index:
            return
        for position, existing in enumerate(self._entries):
            if existing.id == entry_id:
                self._entries[position] = entry
                self._index[entry_id] = entry
                return

    def recent(self, limit: int = 100) -> tuple[AuditEntry, ...]:
        if limit <= 0:
            return ()
        # Newest first: an operator reading an audit log is asking "what just
        # happened", never "what happened first".
        return tuple(self._entries)[-limit:][::-1]

    def __len__(self) -> int:
        return len(self._entries)
