"""Graph read API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from bystack.api.deps import Collectors, Store
from bystack.api.schemas import ContainerLogsOut, EdgeOut, NodeOut, SnapshotOut
from bystack.core.identity import URN, NodeKind, URNError
from bystack.providers.agent.commands import (
    DEFAULT_LOG_TAIL,
    MAX_LOG_TAIL,
    LogsUnavailable,
)

router = APIRouter(prefix="/graph", tags=["graph"])

#: Seconds of silence on a live log before the stream sends a comment.
#:
#: Comfortably inside the shortest read timeout a proxy is likely to impose --
#: nginx's `proxy_read_timeout` defaults to 60s and is the one this is chosen
#: against. It is a bound on how long an *idle* stream can look dead, not a
#: poll: a container that is writing never reaches it.
STREAM_KEEPALIVE: Final = 15.0


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

    One shot. The live tail is its sibling below (`/node/logs/stream`), and
    this survives beside it as the API and as the fallback for an agent too old
    to stream -- `tail` bounds the answer at the daemon, which is what makes it
    safe to ask of a container that has been logging for a month.

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


@router.get(
    "/node/logs/stream",
    summary="A container's log, live",
    response_class=StreamingResponse,
)
async def stream_node_logs(
    store: Store,
    collector: Collectors,
    urn: str = Query(description="Container URN"),
    tail: int = Query(
        default=DEFAULT_LOG_TAIL,
        ge=1,
        le=MAX_LOG_TAIL,
        description="Backfill before the live tail begins.",
    ),
) -> StreamingResponse:
    """Follow one container's output until the caller goes away.

    **Server-sent events, not a WebSocket.** The data goes one way, the
    browser gets reconnection for free, and it is an ordinary GET that any
    reverse proxy in front of the browser port already understands -- which
    is a property ADR-0011 deliberately preserved for this listener. A
    WebSocket would buy a channel back that nothing needs: cancellation is the
    connection closing, which is exactly what SSE gives.

    **A read, like its one-shot sibling.** Not a `CommandKind`, not routed
    through `CommandService`, and answered in full by a read-only Controller.
    Read-only exists so that looking is always allowed.

    Two event types, plus a comment. `lines` carries output and the count of
    anything dropped on the way; `end` says the log is over and why, with a
    `reason` of `null` for an ordinary ending and a string for a failure --
    the client has to tell those apart, because silence after a crash and
    silence after a clean stop look identical otherwise. There is deliberately
    no `error` event: everything that can be refused is refused *before* the
    response begins, and answered with a status code, because a 409 the browser
    can act on beats a 200 whose first event is an apology. A failure that
    arrives later is an `end` with a reason, which is what it is.

    The comment is `: keepalive`, every :data:`STREAM_KEEPALIVE` seconds of
    silence. SSE ignores it by specification; proxies and idle timers do not.
    """
    parsed = _parse_urn(urn)
    if parsed.kind != NodeKind.CONTAINER or len(parsed.segments) != 2:
        raise HTTPException(status_code=400, detail=f"not a container URN: {urn}")
    if store.node(parsed) is None:
        raise HTTPException(status_code=404, detail=f"no such node: {urn}")

    engine_id, container_id = parsed.segments
    provider = collector.agent_provider(engine_id, create=False)
    if provider is None:
        raise HTTPException(
            status_code=404, detail=f"host {engine_id} is not managed by an agent"
        )

    try:
        # Entered here rather than inside the generator so a refusal is still
        # a status code. Once `StreamingResponse` has begun, the headers are
        # gone and the only way left to say "no" is an event nobody may be
        # reading yet.
        subscription = provider.follow(container_id, tail)
        stream = await subscription.__aenter__()
    except LogsUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def events() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    async with asyncio.timeout(STREAM_KEEPALIVE):
                        event = await stream.next()
                except TimeoutError:
                    # A quiet container is the normal case, not an edge one --
                    # most containers log at startup and then say nothing for
                    # hours. Without this the connection carries no bytes at
                    # all in that time, and the reverse proxy this listener was
                    # deliberately kept able to sit behind closes it on its own
                    # read timeout (nginx defaults to 60s). `EventSource` then
                    # reconnects by itself, which re-subscribes, which re-sends
                    # the backfill -- so the operator watching an idle log sees
                    # the same screenful of lines appear again every minute and
                    # the managed host pays for a new `docker logs --follow`
                    # each time. A comment costs one line and no event.
                    yield ": keepalive\n\n"
                    continue
                if event.lines or event.dropped:
                    yield _sse(
                        "lines",
                        {
                            "lines": [
                                {"stderr": line.stderr, "text": line.text}
                                for line in event.lines
                            ],
                            "dropped": event.dropped,
                        },
                    )
                if event.done:
                    yield _sse("end", {"reason": event.reason})
                    return
        finally:
            # Runs when the browser disconnects too: Starlette closes the
            # generator, which is what cancels the subscription and stops
            # `docker logs --follow` on the managed host. That is the whole
            # reason `follow` is a context manager.
            await subscription.__aexit__(None, None, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            # Proxies buffer by default, and a buffered live log is not one.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: object) -> str:
    """One server-sent event.

    `json.dumps` rather than string building because a log line contains
    whatever the process wrote, including newlines -- and a raw newline inside
    an SSE `data:` field ends the field. Encoding it as JSON makes that
    impossible by construction rather than by remembering to escape.
    """
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _parse_urn(raw: str) -> URN:
    try:
        return URN(raw)
    except URNError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
