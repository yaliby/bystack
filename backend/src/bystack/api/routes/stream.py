"""WebSocket delta stream.

Protocol::

    server -> {"type": "snapshot", "seq": N,   "nodes": [...], "edges": [...]}
    server -> {"type": "delta",    "seq": N+1, ...}
    server -> {"type": "delta",    "seq": N+2, ...}

A client applies deltas in sequence order. A gap is unrecoverable by
construction -- the only correct response is to re-read a snapshot -- so the
server takes care never to create one it does not announce.
"""

from __future__ import annotations

import logging
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from bystack.api.deps import AppContext
from bystack.api.schemas import DeltaOut, SnapshotOut
from bystack.core.graph.delta import GraphDelta, GraphSnapshot
from bystack.core.ports.eventbus import Lagged, Topic

log = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

#: Per-client mailbox depth. Sized for a burst (a stack deploy), not for a
#: client that has stopped reading -- that one is meant to be dropped.
CLIENT_QUEUE_DEPTH = 256


@router.websocket("/stream")
async def stream(
    websocket: WebSocket,
    sources: list[str] | None = Query(default=None),
) -> None:
    """Stream the graph: one snapshot, then deltas forever.

    ``sources`` scopes the subscription to specific provider partitions. At
    the stated scale this is a survival condition rather than an
    optimization: a client looking at one host must not be woken for churn on
    the other 499.
    """
    context: AppContext = websocket.app.state.context
    await websocket.accept()
    scope = set(sources) if sources else None

    try:
        while True:
            # Subscribe BEFORE snapshotting. The reverse order leaves a window
            # in which a delta is published after the snapshot is taken but
            # before the subscription exists -- a silently lost change that no
            # sequence check would ever catch, because no gap appears.
            subscription = context.bus.subscribe(
                Topic.GRAPH_DELTA, maxsize=CLIENT_QUEUE_DEPTH
            )

            snapshot = context.store.snapshot()
            await websocket.send_json(_encode_snapshot(snapshot, scope))

            try:
                # aclosing, not a bare `async for`: on disconnect the send
                # below raises and we leave the loop, and the subscription's
                # mailbox must be released deterministically rather than
                # whenever the generator happens to be collected.
                async with aclosing(subscription):
                    async for message in subscription:
                        delta = message
                        if not isinstance(delta, GraphDelta):  # pragma: no cover - defensive
                            continue
                        # Deltas already reflected in the snapshot we just sent.
                        if delta.seq <= snapshot.seq:
                            continue
                        payload = _encode_delta(delta, scope)
                        if payload is not None:
                            await websocket.send_json(payload)
            except Lagged as exc:
                # Policy from ADR: a slow client loses its backlog rather than
                # being given a larger buffer. Recovery is a fresh snapshot,
                # which is bounded and always correct.
                log.info("client lagged (%d dropped); resnapshotting", exc.dropped)
                continue

    except WebSocketDisconnect:
        return
    except RuntimeError:
        # Raised when writing to a socket the client has already closed.
        return


def _encode_snapshot(snapshot: GraphSnapshot, scope: set[str] | None) -> dict[str, Any]:
    out = SnapshotOut.of(snapshot)
    if scope is not None:
        out.nodes = [n for n in out.nodes if n.source in scope]
        out.edges = [e for e in out.edges if e.source in scope]
    return out.model_dump()


def _encode_delta(delta: GraphDelta, scope: set[str] | None) -> dict[str, Any] | None:
    """Project a delta into a client's scope.

    Returns ``None`` when nothing in the delta concerns this client, so a
    scoped client stays completely silent during churn elsewhere.

    Removals are not filtered by source: a removal carries only a URN, and
    dropping one because we cannot cheaply prove it belongs to the client's
    scope would leave a ghost node on their canvas forever. Sending a removal
    for something the client never had is harmless.
    """
    out = DeltaOut.of(delta)
    if scope is None:
        return out.model_dump()

    out.upserted_nodes = [n for n in out.upserted_nodes if n.source in scope]
    out.upserted_edges = [e for e in out.upserted_edges if e.source in scope]

    if not (out.upserted_nodes or out.upserted_edges or out.removed_nodes or out.removed_edges):
        return None
    return out.model_dump()
