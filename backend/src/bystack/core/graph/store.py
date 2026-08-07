"""The graph store: port and in-memory implementation.

The store is the boundary between the collector and everything that reads the
graph. Splitting the collector into its own process later means writing a
second implementation of :class:`GraphStore` -- an RPC or shared-state
adapter -- and changing one line in the composition root. Nothing else moves.

**State model.** This store is ephemeral and rebuildable from providers within
seconds of a cold start. It is never the source of truth. See ADR-0001.

**Concurrency.** All mutation happens on a single asyncio event loop, and no
method here awaits mid-mutation, so no locking is required. Any future
implementation that awaits inside a mutation must add its own serialization.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from bystack.core.graph.delta import GraphDelta, GraphSnapshot
from bystack.core.graph.model import EDGE_OWNER, Edge, EdgeKey, Node
from bystack.core.identity import URN


@runtime_checkable
class GraphStore(Protocol):
    """Read/write access to the canonical infrastructure graph."""

    @property
    def seq(self) -> int:
        """Sequence number of the most recent applied change."""
        ...

    def snapshot(self) -> GraphSnapshot: ...

    def node(self, urn: URN) -> Node | None: ...

    def neighbors(self, urn: URN) -> tuple[Edge, ...]:
        """All edges incident to ``urn``, in either direction."""
        ...

    def upsert(
        self, source: str, nodes: Iterable[Node] = (), edges: Iterable[Edge] = ()
    ) -> GraphDelta:
        """Incremental write. Unchanged entities are silently skipped."""
        ...

    def remove(
        self, source: str, nodes: Iterable[URN] = (), edges: Iterable[EdgeKey] = ()
    ) -> GraphDelta:
        """Incremental delete, scoped to ``source``'s partition."""
        ...

    def reconcile(
        self,
        source: str,
        nodes: Iterable[Node],
        edges: Iterable[Edge],
        kinds: frozenset[str] | None = None,
    ) -> GraphDelta:
        """Declare the complete contents of ``source``'s partition.

        Anything previously owned by ``source`` and absent from this call is
        removed. This is the safety net that repairs state after dropped
        events, and it is the only operation permitted to delete entities the
        provider never explicitly reported as gone.

        ``kinds`` narrows the reconcile to a *slice* of the partition: only
        nodes of those kinds, and only edges **owned** by such nodes (see
        :data:`~bystack.core.graph.model.EDGE_OWNER`), are eligible for
        removal. This is what lets a provider say "here are all my
        containers" in response to one event without claiming anything about
        its networks and volumes -- and it is how an orphaned logical node
        (a service whose last container just died) disappears immediately
        rather than lingering until the next full resync.
        """
        ...


class InMemoryGraphStore:
    """Default :class:`GraphStore`, sized for the stated resource budget.

    Partitioning is the mechanism that enforces provider independence: a
    provider may only write and reconcile its own ``source`` partition, so the
    Docker provider structurally cannot delete a node the Prometheus provider
    contributed -- even by mistake.
    """

    __slots__ = ("_seq", "_nodes", "_edges", "_node_partitions", "_edge_partitions", "_adjacency")

    def __init__(self) -> None:
        self._seq = 0
        self._nodes: dict[URN, Node] = {}
        self._edges: dict[EdgeKey, Edge] = {}
        self._node_partitions: dict[str, set[URN]] = defaultdict(set)
        self._edge_partitions: dict[str, set[EdgeKey]] = defaultdict(set)
        # Adjacency is maintained eagerly because relationship tracing is a
        # core feature, and rebuilding it per query would be O(edges) on a
        # user interaction.
        self._adjacency: dict[URN, set[EdgeKey]] = defaultdict(set)

    # -- reads ------------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._seq

    def snapshot(self) -> GraphSnapshot:
        return GraphSnapshot(
            seq=self._seq, nodes=tuple(self._nodes.values()), edges=tuple(self._edges.values())
        )

    def node(self, urn: URN) -> Node | None:
        return self._nodes.get(urn)

    def neighbors(self, urn: URN) -> tuple[Edge, ...]:
        return tuple(self._edges[key] for key in self._adjacency.get(urn, ()))

    def __len__(self) -> int:
        return len(self._nodes)

    # -- writes -----------------------------------------------------------

    def upsert(
        self, source: str, nodes: Iterable[Node] = (), edges: Iterable[Edge] = ()
    ) -> GraphDelta:
        changed_nodes = [n for n in nodes if self._put_node(source, n)]
        changed_edges = [e for e in edges if self._put_edge(source, e)]
        return self._commit(
            upserted_nodes=tuple(changed_nodes), upserted_edges=tuple(changed_edges)
        )

    def remove(
        self, source: str, nodes: Iterable[URN] = (), edges: Iterable[EdgeKey] = ()
    ) -> GraphDelta:
        # Removing a node orphans its edges, so they go too -- an edge to a
        # node that no longer exists is not a relationship, it is a leak.
        dropped_edges: set[EdgeKey] = {key for key in edges if key in self._edges}
        released: list[URN] = []
        gone: list[URN] = []
        for urn in nodes:
            if urn not in self._nodes or urn not in self._node_partitions[source]:
                continue
            released.append(urn)
            if self._claimed_elsewhere(source, urn):
                continue
            gone.append(urn)
            dropped_edges.update(self._adjacency.get(urn, ()))

        for key in dropped_edges:
            self._drop_edge(key)
        for urn in released:
            self._drop_node(source, urn)

        return self._commit(removed_nodes=tuple(gone), removed_edges=tuple(dropped_edges))

    def reconcile(
        self,
        source: str,
        nodes: Iterable[Node],
        edges: Iterable[Edge],
        kinds: frozenset[str] | None = None,
    ) -> GraphDelta:
        nodes = tuple(nodes)
        edges = tuple(edges)

        changed_nodes = tuple(n for n in nodes if self._put_node(source, n))
        changed_edges = tuple(e for e in edges if self._put_edge(source, e))

        present_nodes = {n.urn for n in nodes}
        present_edges = {e.key for e in edges}

        stale_nodes = tuple(
            urn
            for urn in self._node_partitions[source] - present_nodes
            if kinds is None or urn.kind in kinds
        )
        # Only the last claimant of a shared URN actually removes it. Anything
        # another partition still declares is merely released here, and must
        # not cascade into that partition's edges.
        gone = tuple(urn for urn in stale_nodes if not self._claimed_elsewhere(source, urn))

        stale_edges = {
            key
            for key in self._edge_partitions[source] - present_edges
            if kinds is None or self._edge_in_scope(key, kinds)
        }

        for urn in gone:
            stale_edges.update(self._adjacency.get(urn, ()))
        for key in stale_edges:
            self._drop_edge(key)
        for urn in stale_nodes:
            self._drop_node(source, urn)

        return self._commit(
            upserted_nodes=changed_nodes,
            upserted_edges=changed_edges,
            removed_nodes=gone,
            removed_edges=tuple(stale_edges),
        )

    # -- internals --------------------------------------------------------

    def _edge_in_scope(self, key: EdgeKey, kinds: frozenset[str]) -> bool:
        """Is this edge eligible for removal by a kind-scoped reconcile?

        An edge belongs to the slice of its *owning* endpoint -- the dependent
        one, per :data:`EDGE_OWNER`. So a container-scoped reconcile reclaims
        ``host -> container`` (owned by the container) and
        ``container -> network`` (declared by the container), while a
        network-scoped reconcile reclaims only ``host -> network`` and leaves
        the attachments alone, because it never declares them.

        Using "either endpoint is in scope" here instead produces an infinite
        flap: the network slice deletes attachments it does not declare, and
        the container slice restores them, forever.
        """
        edge = self._edges.get(key)
        if edge is None:
            return False
        owner = edge.dst if EDGE_OWNER.get(edge.kind, "src") == "dst" else edge.src
        return owner.kind in kinds

    def _put_node(self, source: str, node: Node) -> bool:
        """Insert or update. Returns whether anything actually changed."""
        existing = self._nodes.get(node.urn)
        self._node_partitions[source].add(node.urn)
        if existing is not None and existing.same_content_as(node):
            # Content-identical. Skipping here is what keeps a steady-state
            # cluster at zero WebSocket traffic across every reconcile.
            return False
        self._nodes[node.urn] = node
        return True

    def _put_edge(self, source: str, edge: Edge) -> bool:
        self._edge_partitions[source].add(edge.key)
        existing = self._edges.get(edge.key)
        if existing is not None:
            # Endpoints and kind are baked into the key, so only attrs can
            # differ -- a container keeping its network attachment but being
            # assigned a new IP, for instance.
            if existing.attrs == edge.attrs:
                return False
            self._edges[edge.key] = edge
            return True
        self._edges[edge.key] = edge
        self._adjacency[edge.src].add(edge.key)
        self._adjacency[edge.dst].add(edge.key)
        return True

    def _claimed_elsewhere(self, source: str, urn: URN) -> bool:
        """Does another partition still declare this node?

        Almost every URN belongs to exactly one partition, because almost
        every URN is engine-scoped. Image identity deliberately is not: it is
        the content digest, so two hosts running the same image arrive at the
        same URN independently -- and that coincidence *is* the cross-host
        correlation (ADR-0002), not a collision to resolve.

        Which makes it the one place a partition can reach outside itself.
        Deleting the node on the first claimant's say-so removes an image
        other hosts are still running and takes their ``uses_image`` edges
        with it -- a cross-partition write, performed by the very mechanism
        that exists to make cross-partition writes impossible (ADR-0003).
        Under the agent model that stops being hygiene: one `docker image
        prune` on a compromised host would blank part of every other host's
        graph, silently, with no error anywhere.
        """
        return any(
            urn in owned for other, owned in self._node_partitions.items() if other != source
        )

    def _drop_node(self, source: str, urn: URN) -> None:
        """Release this partition's claim; delete only if it was the last."""
        self._node_partitions[source].discard(urn)
        if any(urn in owned for owned in self._node_partitions.values()):
            return
        self._nodes.pop(urn, None)
        self._adjacency.pop(urn, None)

    def _drop_edge(self, key: EdgeKey) -> None:
        edge = self._edges.pop(key, None)
        if edge is None:
            return
        for partition in self._edge_partitions.values():
            partition.discard(key)
        for endpoint in (edge.src, edge.dst):
            adjacent = self._adjacency.get(endpoint)
            if adjacent is not None:
                adjacent.discard(key)
                if not adjacent:
                    self._adjacency.pop(endpoint, None)

    def _commit(self, **changes: tuple[Any, ...]) -> GraphDelta:
        # The element types differ per keyword (nodes, URNs, edges, edge keys)
        # and `**kwargs` cannot express that; the real check is GraphDelta's
        # own field types, against the explicit keywords the callers pass.
        delta = GraphDelta(seq=self._seq + 1, **changes)
        if delta.is_empty:
            # Do not burn a sequence number on a no-op: clients use sequence
            # continuity to detect gaps, and phantom increments would make a
            # quiet system look like a lossy one.
            return GraphDelta(seq=self._seq)
        self._seq = delta.seq
        return delta
