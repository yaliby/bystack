"""Graph read API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from bystack.api.deps import Store
from bystack.api.schemas import EdgeOut, NodeOut, SnapshotOut
from bystack.core.identity import URN, URNError

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


def _parse_urn(raw: str) -> URN:
    try:
        return URN(raw)
    except URNError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
