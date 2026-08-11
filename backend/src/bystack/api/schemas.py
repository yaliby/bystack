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
from bystack.core.identity import process_urn, unit_urn
from bystack.core.ports.command import (
    AuditEntry,
    CommandKind,
    CommandResult,
    TargetOutcome,
)
from bystack.core.ports.provider import ProviderHealth
from bystack.core.ports.watch import MAX_PATTERN, MatchKind, WatchEntry, WatchKind
from bystack.providers.agent.commands import InventoryResult, LogsResult
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
    version: str
    """What this Controller is, so an agent's version means something.

    Every agent reports its own at `Hello` and it has been carried on
    `GET /agents` for a while, next to nothing to compare it against -- which
    made it a fact rather than an answer. A mixed-version fleet is a normal
    operating state under ADR-0008, so "which hosts are behind" is a question
    an operator asks routinely and could not previously ask here at all
    (ADR-0015).
    """

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
    # which is worse than no attribution at all: it looks like evidence.
    #
    # It stays absent. ADR-0014 decides there is no session for it to be
    # populated from -- one operator, one LAN, no user model -- so every entry
    # reads `anonymous` and says plainly that this platform does not know.


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


class LogLineOut(BaseModel):
    """One line, and which stream it came out of."""

    stderr: bool
    text: str


class ContainerLogsOut(BaseModel):
    """The tail of a container's log, or a reason there is none.

    ``ok`` is what separates "this container has written nothing" from "we
    could not ask" -- two situations that render identically as an empty list
    and mean nothing like the same thing to whoever is debugging.
    """

    target: str
    ok: bool
    reason: str | None = None
    lines: list[LogLineOut] = []

    @classmethod
    def of(cls, target: str, result: LogsResult) -> ContainerLogsOut:
        return cls.model_construct(
            target=target,
            ok=result.ok,
            reason=result.reason,
            lines=[
                LogLineOut.model_construct(stderr=line.stderr, text=line.text)
                for line in result.lines
            ],
        )


class InventoryItemOut(BaseModel):
    """One thing that could be watched on a host."""

    id: str = Field(description="What a watch entry would name: a unit name or an exec path")
    name: str
    description: str = ""
    state: str = ""
    detail: str = ""
    pid: int = 0


class InventoryOut(BaseModel):
    """The picker's contents, or a reason there are none.

    ``ok`` separates "this machine has no unit matching that text" from "the
    agent is asleep", which render identically as an empty list and mean
    nothing like the same thing to whoever is looking at it.
    """

    engine_id: str
    kind: str
    ok: bool
    reason: str | None = None
    items: list[InventoryItemOut] = []
    total: int = 0
    """Matches before the agent's cap, so a truncated list is never presented
    as the whole truth."""

    @classmethod
    def of(cls, engine_id: str, kind: str, result: InventoryResult) -> InventoryOut:
        return cls.model_construct(
            engine_id=engine_id,
            kind=kind,
            ok=result.ok,
            reason=result.reason,
            items=[
                InventoryItemOut.model_construct(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    state=item.state,
                    detail=item.detail,
                    pid=item.pid,
                )
                for item in result.items
            ],
            total=result.total,
        )


class WatchEntryIn(BaseModel):
    """A thing to start watching.

    Both shapes in one model, because the two differ by three fields and a
    pair of endpoints would have to be kept in step forever. Which fields are
    required is decided by ``kind`` in the kernel
    (:func:`~bystack.core.ports.watch.normalize`), not here: a rule that lived
    in Pydantic would be a second copy of it, and the copy that runs first
    would win by accident.

    ``id`` is deliberately absent. A client that could choose one could
    overwrite somebody else's entry by guessing it, and it is free not to
    allow.
    """

    model_config = {"extra": "forbid"}

    kind: WatchKind
    name: str = Field(
        default="",
        max_length=MAX_PATTERN,
        description="Unit name, for kind=unit. `.service` is appended if no suffix is given.",
    )
    match_kind: MatchKind | None = Field(
        default=None, description="How to match, for kind=process"
    )
    pattern: str = Field(
        default="",
        max_length=MAX_PATTERN,
        description=(
            "What to match, for kind=process. A substring or an exact value, never a regex."
        ),
    )
    label: str = Field(
        default="",
        max_length=100,
        description="What to call it on the map. Optional; the name or pattern is used otherwise.",
    )


class WatchEntryOut(BaseModel):
    """A stored watch entry, in the form the graph will speak about it."""

    id: str
    engine_id: str
    kind: str
    name: str = ""
    match_kind: str | None = None
    pattern: str = ""
    label: str = ""
    added_at: float = 0.0
    urn: str = Field(description="The node this entry becomes, whether or not it exists yet")

    @classmethod
    def of(cls, entry: WatchEntry) -> WatchEntryOut:
        return cls.model_construct(
            id=entry.id,
            engine_id=entry.engine_id,
            kind=str(entry.kind),
            name=entry.name,
            match_kind=str(entry.match_kind) if entry.match_kind else None,
            pattern=entry.pattern,
            label=entry.label,
            added_at=entry.added_at,
            urn=str(
                unit_urn(entry.engine_id, entry.name)
                if entry.kind is WatchKind.UNIT
                else process_urn(entry.engine_id, entry.id)
            ),
        )


class WatchListOut(BaseModel):
    """Everything one host is watching, and whether it has been told.

    ``delivered`` is the honest half. The list is the Controller's and it is
    durable, so an edit takes effect whether or not the host is reachable --
    but an operator who just added a service is entitled to know that the
    machine has not heard about it yet, rather than watching a card that never
    appears.
    """

    engine_id: str
    entries: list[WatchEntryOut] = []
    delivered: bool = False
    detail: str | None = None


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
