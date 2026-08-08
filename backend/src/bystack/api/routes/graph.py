"""Graph read API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from bystack.api.deps import Collectors, Store
from bystack.api.schemas import ContainerLogsOut, EdgeOut, NodeOut, SnapshotOut
from bystack.core.identity import URN, NodeKind, URNError
from bystack.providers.agent.commands import DEFAULT_LOG_TAIL, MAX_LOG_TAIL

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("", response_model=SnapshotOut, summary="Full graph snapshot")
async def get_graph(
    store: Store,
    sources: list[str] | None = Query(
        default=None,
        description="Restrict to these provider partitions. Omit for everything.",
    ),
) -> SnapshotOut:
    """Return the current graph.

    This is the entry point for every client and the recovery path for a
    client that fell behind the delta stream. It is intentionally cheap
    enough to be used that way -- the graph is held in memory and the
    response is a projection of it, not a query against anything.
    """
    snapshot = SnapshotOut.of(store.snapshot())
    if sources:
        allowed = set(sources)
        snapshot.nodes = [n for n in snapshot.nodes if n.source in allowed]
        snapshot.edges = [e for e in snapshot.edges if e.source in allowed]
    return snapshot


# URNs are carried as query parameters rather than path segments. They
# contain both ':' and '/', so as a path segment every one of them would need
# escaping on the client, a `:path` converter on the server, and a greedy
# match that then collides with any suffix route. A query parameter has none
# of those problems and keeps the identifier readable in logs.


@router.get("/node", response_model=NodeOut, summary="One node")
async def get_node(store: Store, urn: str = Query(description="Node URN")) -> NodeOut:
    node = store.node(_parse_urn(urn))
    if node is None:
        raise HTTPException(status_code=404, detail=f"no such node: {urn}")
    return NodeOut.of(node)


@router.get(
    "/node/edges",
    response_model=list[EdgeOut],
    summary="Edges incident to a node",
)
async def get_node_edges(store: Store, urn: str = Query(description="Node URN")) -> list[EdgeOut]:
    """All relationships touching a node, in either direction.

    The primitive behind relationship tracing in the UI: "what does this
    depend on, and what depends on it" is one call per hop, served from the
    adjacency index rather than a scan.
    """
    parsed = _parse_urn(urn)
    if store.node(parsed) is None:
        raise HTTPException(status_code=404, detail=f"no such node: {urn}")
    return [EdgeOut.of(edge) for edge in store.neighbors(parsed)]


@router.get(
    "/node/logs",
    response_model=ContainerLogsOut,
    summary="The tail of a container's log",
)
async def get_node_logs(
    store: Store,
    collector: Collectors,
    urn: str = Query(description="Container URN"),
    tail: int = Query(
        default=DEFAULT_LOG_TAIL,
        ge=1,
        le=MAX_LOG_TAIL,
        description="Lines from the end. Clamped again by the agent.",
    ),
) -> ContainerLogsOut:
    """Read one container's recent output, through its host's agent.

    A read, and deliberately not an operation: it does not go through
    `CommandService`, it is not a `CommandKind`, and a read-only Controller
    answers it in full. Refusing to show an operator why a container is
    failing because the platform is in its safe mode would be exactly
    backwards -- read-only exists so that looking is always allowed.

    One shot, not a stream. `tail` bounds the answer at the daemon, which is
    what makes this safe to ask of a container that has been logging for a
    month. Following a live log is a different feature with a different
    backpressure problem.

    A host whose agent is asleep is answered rather than 404'd: the reason
    travels in the body, so the UI can say "this host is offline" instead of
    rendering an error over a container the operator can plainly see.
    """
    parsed = _parse_urn(urn)
    if parsed.kind != NodeKind.CONTAINER or len(parsed.segments) != 2:
        raise HTTPException(status_code=400, detail=f"not a container URN: {urn}")
    if store.node(parsed) is None:
        raise HTTPException(status_code=404, detail=f"no such node: {urn}")

    engine_id, container_id = parsed.segments
    provider = collector.agent_provider(engine_id, create=False)
    if provider is None:
        # The node is in the graph but its partition has no agent-backed
        # provider at all. Distinct from a disconnected agent, which is
        # answered with a reason: this one cannot become true by waiting.
        raise HTTPException(
            status_code=404, detail=f"host {engine_id} is not managed by an agent"
        )

    return ContainerLogsOut.of(str(parsed), await provider.logs(container_id, tail))


def _parse_urn(raw: str) -> URN:
    try:
        return URN(raw)
    except URNError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
