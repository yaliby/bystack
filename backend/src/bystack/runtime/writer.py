"""Partition writer.

The bridge between a provider and the rest of the system. Every write goes
store-then-bus, in that order: the store is authoritative, and publishing a
delta describing state a reader could not yet observe would let a client
apply a change and then read a snapshot that contradicts it.
"""

from __future__ import annotations

from collections.abc import Iterable

from bystack.core.graph.delta import GraphDelta
from bystack.core.graph.model import Edge, EdgeKey, Node
from bystack.core.graph.store import GraphStore
from bystack.core.identity import URN
from bystack.core.ports.eventbus import EventBus, Topic


class PartitionWriter:
    """A :class:`~bystack.core.ports.provider.GraphWriter` bound to one source.

    The binding is the enforcement mechanism for provider isolation. A
    provider holding one of these has no parameter through which to name
    another provider's partition, so "no provider depends on another
    provider" is a property of the type signature rather than a rule someone
    has to remember.
    """

    __slots__ = ("_store", "_bus", "_source")

    def __init__(self, store: GraphStore, bus: EventBus, source: str) -> None:
        self._store = store
        self._bus = bus
        self._source = source

    async def upsert(self, nodes: Iterable[Node] = (), edges: Iterable[Edge] = ()) -> None:
        await self._publish(self._store.upsert(self._source, nodes, edges))

    async def remove(self, nodes: Iterable[URN] = (), edges: Iterable[EdgeKey] = ()) -> None:
        await self._publish(self._store.remove(self._source, nodes, edges))

    async def reconcile(
        self,
        nodes: Iterable[Node],
        edges: Iterable[Edge],
        kinds: frozenset[str] | None = None,
    ) -> None:
        await self._publish(self._store.reconcile(self._source, nodes, edges, kinds))

    async def _publish(self, delta: GraphDelta) -> None:
        # An unchanged reconcile produces an empty delta, and empty deltas
        # never reach the wire. This is what keeps an idle cluster at exactly
        # zero WebSocket traffic despite reconciling on a timer.
        if delta.is_empty:
            return
        await self._bus.publish(Topic.GRAPH_DELTA, delta)
