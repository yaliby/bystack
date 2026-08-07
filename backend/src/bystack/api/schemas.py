"""Wire schemas.

The Pydantic boundary. Everything inward of this module uses lightweight
kernel types; everything outward is validated, documented and OpenAPI-visible.

These models are built with ``model_construct`` rather than validation. The
data originates in our own kernel, has already been through the mapper, and
is immutable -- re-validating thousands of nodes on every snapshot would buy
nothing and cost real CPU against a budget that does not have it to spare.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from bystack.core.graph.delta import GraphDelta, GraphSnapshot
from bystack.core.graph.model import Edge, Node
from bystack.core.ports.command import (
    AuditEntry,
    CommandKind,
    CommandResult,
    TargetOutcome,
)
from bystack.core.ports.provider import ProviderHealth
from bystack.runtime.commands import AvailableActions


class NodeOut(BaseModel):
    urn: str
    kind: str
    name: str
    source: str
    status: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    attrs: dict[str, Any] = Field(default_factory=dict)
    observed_at: float = 0.0
    revision: str = ""

    @classmethod
    def of(cls, node: Node) -> NodeOut:
        return cls.model_construct(
            urn=str(node.urn),
            kind=node.kind,
            name=node.name,
            source=node.source,
            status=node.status,
            labels=dict(node.labels),
            attrs=dict(node.attrs),
            observed_at=node.observed_at,
            revision=node.revision,
        )


class EdgeOut(BaseModel):
    key: str
    kind: str
    src: str
    dst: str
    source: str
    attrs: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, edge: Edge) -> EdgeOut:
        return cls.model_construct(
            key=edge.key,
            kind=str(edge.kind),
            src=str(edge.src),
            dst=str(edge.dst),
            source=edge.source,
            attrs=dict(edge.attrs),
        )


class SnapshotOut(BaseModel):
    """A complete graph, valid as of ``seq``."""

    type: Literal["snapshot"] = "snapshot"
    seq: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]

    @classmethod
    def of(cls, snapshot: GraphSnapshot) -> SnapshotOut:
        return cls.model_construct(
            type="snapshot",
            seq=snapshot.seq,
            nodes=[NodeOut.of(n) for n in snapshot.nodes],
            edges=[EdgeOut.of(e) for e in snapshot.edges],
        )


class DeltaOut(BaseModel):
    """An incremental change.

    Clients apply deltas in ``seq`` order. A gap means messages were dropped
    and the only correct recovery is to re-read a snapshot -- never to guess
    at the missing changes.
    """

    type: Literal["delta"] = "delta"
    seq: int
    upserted_nodes: list[NodeOut] = Field(default_factory=list)
    removed_nodes: list[str] = Field(default_factory=list)
    upserted_edges: list[EdgeOut] = Field(default_factory=list)
    removed_edges: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, delta: GraphDelta) -> DeltaOut:
        return cls.model_construct(
            type="delta",
            seq=delta.seq,
            upserted_nodes=[NodeOut.of(n) for n in delta.upserted_nodes],
            removed_nodes=[str(u) for u in delta.removed_nodes],
            upserted_edges=[EdgeOut.of(e) for e in delta.upserted_edges],
            removed_edges=list(delta.removed_edges),
        )


class ProviderHealthOut(BaseModel):
    id: str
    kind: str
    state: str
    detail: str | None = None
    last_sync_at: float = 0.0
    node_count: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, provider_id: str, kind: str, health: ProviderHealth) -> ProviderHealthOut:
        return cls.model_construct(
            id=provider_id,
            kind=kind,
            state=str(health.state),
            detail=health.detail,
            last_sync_at=health.last_sync_at,
            node_count=health.node_count,
            metrics={k: str(v) for k, v in health.metrics.items()},
        )


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    seq: int
    node_count: int
    edge_count: int
    read_only: bool
    providers: list[ProviderHealthOut]


# --------------------------------------------------------------------------
# Operations
#
# Unlike the graph schemas above, the request model here *is* validated --
# it is the one place in the API where input originates outside the platform
# rather than inside it, and it is the input to an operation that changes
# real infrastructure.
# --------------------------------------------------------------------------


class CommandIn(BaseModel):
    """A requested operation."""

    model_config = {"extra": "forbid"}

    kind: CommandKind
    target: str = Field(description="URN of a container, service or stack")

    timeout: float | None = Field(
        default=None,
        ge=0,
        le=600,
        description="Grace period for stop/restart. Omit to use the engine's own default.",
    )
    signal: str | None = Field(
        default=None,
        max_length=16,
        description="Signal for kill, e.g. SIGTERM. Omit for SIGKILL.",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Recorded verbatim in the audit trail. Never interpreted.",
    )

    # `actor` is deliberately absent. Accepting one from the client would put
    # an attacker-chosen name in the audit log next to a real operation,
    # which is worse than no attribution at all: it looks like evidence. The
    # field appears when authentication does, populated from the session.


class TargetOutcomeOut(BaseModel):
    target: str
    status: str
    detail: str | None = None
    duration_ms: int = 0

    @classmethod
    def of(cls, outcome: TargetOutcome) -> TargetOutcomeOut:
        return cls.model_construct(
            target=str(outcome.target),
            status=str(outcome.status),
            detail=outcome.detail,
            duration_ms=outcome.duration_ms,
        )


class CommandResultOut(BaseModel):
    """What happened, per target.

    Returned with ``200`` even when targets failed. The request was accepted,
    authorized and carried out; that some containers refused is a fact about
    the infrastructure, and it belongs in the body where the client can show
    all of it. Collapsing six outcomes into one status code would throw away
    the only information the operator needs.
    """

    id: str
    kind: str
    target: str
    status: str
    actor: str
    requested_at: float
    duration_ms: int
    outcomes: list[TargetOutcomeOut]

    @classmethod
    def of(cls, result: CommandResult) -> CommandResultOut:
        return cls.model_construct(
            id=result.id,
            kind=str(result.kind),
            target=str(result.target),
            status=str(result.status),
            actor=result.actor,
            requested_at=result.requested_at,
            duration_ms=result.duration_ms,
            outcomes=[TargetOutcomeOut.of(o) for o in result.outcomes],
        )


class ActionsOut(BaseModel):
    """What an operator may do to a node, and why not, when nothing."""

    target: str
    kind: str
    targets: list[str]
    commands: list[str]
    reason: str | None = None
    detail: str | None = None

    @classmethod
    def of(cls, actions: AvailableActions) -> ActionsOut:
        return cls.model_construct(
            target=str(actions.target),
            kind=actions.kind,
            targets=[str(u) for u in actions.targets],
            commands=[str(k) for k in actions.commands],
            reason=str(actions.reason) if actions.reason else None,
            detail=actions.detail,
        )


class AuditEntryOut(BaseModel):
    id: str
    at: float
    actor: str
    kind: str
    target: str
    targets: list[str]
    status: str
    detail: str | None = None
    reason: str | None = None
    duration_ms: int = 0

    @classmethod
    def of(cls, entry: AuditEntry) -> AuditEntryOut:
        return cls.model_construct(
            id=entry.id,
            at=entry.at,
            actor=entry.actor,
            kind=str(entry.kind),
            target=str(entry.target),
            targets=[str(u) for u in entry.targets],
            status=str(entry.status),
            detail=entry.detail,
            reason=entry.reason,
            duration_ms=entry.duration_ms,
        )
