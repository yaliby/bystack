"""Incremental graph changes.

Every mutation of the graph produces a :class:`GraphDelta` carrying a
monotonic sequence number. Clients hold a snapshot plus the sequence they are
current to; a gap in the sequence is detectable, and the only recovery is to
re-request a snapshot. This is what makes "no full refreshes" safe -- we can
always prove whether a client is still in sync.
"""

from __future__ import annotations

from dataclasses import dataclass

from bystack.core.graph.model import Edge, EdgeKey, Node
from bystack.core.identity import URN


@dataclass(frozen=True, slots=True)
class GraphDelta:
    """A single atomic change set."""

    seq: int
    upserted_nodes: tuple[Node, ...] = ()
    removed_nodes: tuple[URN, ...] = ()
    upserted_edges: tuple[Edge, ...] = ()
    removed_edges: tuple[EdgeKey, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when nothing actually changed.

        Empty deltas are the common case during periodic reconciliation and
        must never reach the wire -- a quiet cluster should produce zero
        WebSocket traffic, not a heartbeat of no-op updates.
        """
        return not (
            self.upserted_nodes or self.removed_nodes or self.upserted_edges or self.removed_edges
        )

    @property
    def change_count(self) -> int:
        return (
            len(self.upserted_nodes)
            + len(self.removed_nodes)
            + len(self.upserted_edges)
            + len(self.removed_edges)
        )


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """A consistent point-in-time view, valid as of ``seq``."""

    seq: int
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
