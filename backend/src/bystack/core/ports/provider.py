"""Provider port and the partition writer that enforces provider isolation.

Every external system -- Docker, Prometheus, Grafana, SSH, Wake-on-LAN --
becomes an independent provider. The rule "no provider depends on another
provider" is enforced structurally rather than by convention:

A provider is handed a :class:`GraphWriter` that is **pre-bound to its own
partition**. There is no ``source`` argument on any write method, so a
provider has no way to name another provider's partition, and therefore no
way to corrupt it. Correlation between providers happens above them, by URN,
in the correlation layer -- never by one provider reaching into another.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from bystack.core.graph.model import Edge, EdgeKey, Node
from bystack.core.identity import URN


class ProviderState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    SYNCING = "syncing"
    """Initial List in progress; the partition is not yet complete."""

    READY = "ready"
    """Watch established and the partition is authoritative."""

    DEGRADED = "degraded"
    """Reachable but not fully functional -- e.g. watch dropped, falling back
    to reconciliation only. The graph is stale but not wrong."""

    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    state: ProviderState
    detail: str | None = None
    last_sync_at: float = 0.0
    node_count: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GraphWriter(Protocol):
    """Write access to exactly one partition of the graph."""

    async def upsert(self, nodes: Iterable[Node] = (), edges: Iterable[Edge] = ()) -> None:
        """Incremental write. Content-identical entities are dropped for free."""
        ...

    async def remove(
        self, nodes: Iterable[URN] = (), edges: Iterable[EdgeKey] = ()
    ) -> None: ...

    async def reconcile(
        self,
        nodes: Iterable[Node],
        edges: Iterable[Edge],
        kinds: frozenset[str] | None = None,
    ) -> None:
        """Declare the complete partition contents.

        Anything owned by this provider and absent from the call is removed.
        This is the periodic safety net that repairs whatever the event stream
        dropped. Calling it on a partial result set will delete live
        infrastructure from the graph -- only ever pass a complete List.

        ``kinds`` restricts both the claim and the removals to a slice of the
        partition, so a provider can declare "all of my containers" without
        implying anything about its networks.
        """
        ...


@runtime_checkable
class Provider(Protocol):
    """An independently startable source of infrastructure truth."""

    @property
    def id(self) -> str:
        """Unique instance id; also the graph partition key."""
        ...

    @property
    def kind(self) -> str:
        """Provider type, e.g. ``docker``. Used for registry lookup."""
        ...

    async def start(self) -> None:
        """Begin discovery. Must return promptly, doing work in background
        tasks: one unreachable host must never delay startup of the others."""
        ...

    async def stop(self) -> None:
        """Stop discovery and release resources. Safe to call when stopped."""
        ...

    def health(self) -> ProviderHealth: ...
