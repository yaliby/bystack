"""Choosing what a host should watch.

Three routes and one idea: the operator picks, the Controller remembers, and
the agent observes exactly what it was told to. Everything the Docker slices
get for free — an inventory small enough to draw, a membership that is simply
"whatever is there" — has to be *decided* here, because a machine has two
thousand units and nobody wants a map of them.

    GET    /agents/{engine}/inventory   what could I watch?   (asked of the host)
    GET    /agents/{engine}/watch       what am I watching?   (asked of ourselves)
    POST   /agents/{engine}/watch       watch this too.
    DELETE /agents/{engine}/watch/{id}  stop watching that.

**The inventory is a read of the machine; the watch list is a read of us.**
That is why only the first can fail with the host asleep, and why the second
answers instantly for a host that has been offline for a week. Keeping them
apart is what lets an operator edit a selection for a machine that is not
currently reachable — which is the normal case for the laptops and home servers
ADR-0008 is written around.

**Editing is not a `CommandKind`, and this is not `CommandService`.** Nothing
here changes anything on a host: adding an entry adds a card to a map, and
removing one removes it. The service goes on running exactly as it was. A
read-only Controller therefore edits watch lists happily, for the same reason
it serves logs — read-only bounds what the platform may *do to* a machine, and
choosing what to look at is not that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from bystack.api.deps import Collectors, Context
from bystack.api.schemas import (
    InventoryOut,
    WatchEntryIn,
    WatchEntryOut,
    WatchListOut,
)
from bystack.core.identity import engine_scope
from bystack.core.ports.watch import WatchEntry, WatchKind, WatchRejected
from bystack.providers.agent.commands import DEFAULT_INVENTORY, MAX_INVENTORY

router = APIRouter(prefix="/agents", tags=["watch"])


@router.get(
    "/{engine_id}/inventory",
    response_model=InventoryOut,
    summary="What could be watched on a host",
)
async def get_inventory(
    collector: Collectors,
    engine_id: str,
    kind: WatchKind = Query(default=WatchKind.UNIT, description="unit or process"),
    filter: str = Query(  # noqa: A002 - the query parameter is named for the UI, not for Python
        default="",
        max_length=100,
        description="Case-insensitive substring, applied on the host.",
    ),
    limit: int = Query(default=DEFAULT_INVENTORY, ge=1, le=MAX_INVENTORY),
) -> InventoryOut:
    """Enumerate one machine, once, because somebody opened the picker.

    **On demand, never on a timer.** This is the only frame in the protocol
    that walks a whole machine, and the entire difference between this feature
    and a monitoring agent is that nobody pays for the walk until somebody asks
    to see it. Do not call it on selection, on a poll, or to keep a cache warm
    — `docs/OPEN-WORK.md` records the same rule for the logs panel, and for the
    same reason: clicking through a canvas would issue one round trip per card.

    The filter is applied **at the agent**, so a 2,000-unit host does not put
    its whole unit table on somebody's home uplink for a text box to discard
    1,990 of them.

    A host with no agent-backed provider is a 404, because that cannot become
    true by waiting. A host whose agent is merely asleep is answered with
    `ok: false` and a reason, because it can.
    """
    provider = collector.agent_provider(engine_scope(engine_id), create=False)
    if provider is None:
        raise HTTPException(
            status_code=404, detail=f"host {engine_id} is not managed by an agent"
        )
    result = await provider.inventory(str(kind), filter, limit)
    return InventoryOut.of(engine_scope(engine_id), str(kind), result)


@router.get(
    "/{engine_id}/watch",
    response_model=WatchListOut,
    summary="What a host is watching",
)
async def get_watchlist(context: Context, collector: Collectors, engine_id: str) -> WatchListOut:
    """The stored selection. Answered without touching the host.

    ``delivered`` reports whether the agent currently holds this list rather
    than re-sending it: this is a GET, and a read that pushed configuration as
    a side effect would make refreshing a page an operation.
    """
    scoped = engine_scope(engine_id)
    entries = context.watchlist.entries(scoped)
    provider = collector.agent_provider(scoped, create=False)
    return WatchListOut(
        engine_id=scoped,
        entries=[WatchEntryOut.of(entry) for entry in entries],
        delivered=provider is not None and provider.connected,
        detail=(
            None
            if provider is not None and provider.connected
            else "this host is not connected; it will be told when it reconnects"
        ),
    )


@router.post(
    "/{engine_id}/watch",
    response_model=WatchListOut,
    status_code=201,
    summary="Watch something else on a host",
)
async def add_watch(
    context: Context, collector: Collectors, engine_id: str, body: WatchEntryIn
) -> WatchListOut:
    """Add one entry, store it, and tell the host.

    The host is told *after* the store has accepted, and a host that cannot be
    told is not an error. The list is ours and it is durable; a machine that is
    asleep learns about the entry when it reconnects, and the answer says so
    rather than pretending the edit failed.

    **The entry is not created against anything.** A unit that is not installed
    and a pattern that matches nothing are both legitimate, and both produce a
    node that says what it is — `not-found`, or `absent`. Validating against
    the host's inventory here would mean a service could only be watched while
    it existed, which is exactly backwards for the thing an operator adds
    *because* it keeps disappearing.
    """
    scoped = engine_scope(engine_id)
    try:
        await context.watchlist.add(
            WatchEntry(
                id="",
                engine_id=scoped,
                kind=body.kind,
                name=body.name,
                match_kind=body.match_kind,
                pattern=body.pattern,
                label=body.label,
            )
        )
    except WatchRejected as exc:
        # 422 rather than 400: the request was understood and its shape was
        # wrong, which is what the client's form needs to hear. The message is
        # written to be shown to whoever typed it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await _push(context, collector, scoped)


@router.delete(
    "/{engine_id}/watch/{entry_id}",
    response_model=WatchListOut,
    summary="Stop watching something",
)
async def remove_watch(
    context: Context, collector: Collectors, engine_id: str, entry_id: str
) -> WatchListOut:
    """Forget one entry, and tell the host to stop looking.

    Not a destructive operation in the ADR-0012 sense, and the distinction is
    not a technicality: this deletes a *preference*, on the Controller. The
    unit stays installed, the process stays running, and the only thing that
    disappears is a card. That is why it exists at all in a platform whose
    command set deliberately cannot delete anything.
    """
    scoped = engine_scope(engine_id)
    if not await context.watchlist.remove(scoped, entry_id):
        raise HTTPException(status_code=404, detail=f"no such watch entry: {entry_id}")
    return await _push(context, collector, scoped)


async def _push(context: Context, collector: Collectors, engine_id: str) -> WatchListOut:
    """Send the whole list to the host, and report what happened.

    Complete rather than incremental, matching the frame: the agent is told
    what to watch, not what changed, so a delivery that goes missing costs a
    stale list rather than an entry that is silently never removed.
    """
    entries = context.watchlist.entries(engine_id)
    provider = collector.agent_provider(engine_id, create=False)

    delivered = False
    detail: str | None = None
    if provider is None:
        detail = "no agent has ever connected for this host"
    elif not provider.connected:
        detail = "this host is not connected; it will be told when it reconnects"
    else:
        delivered = await provider.send_watchlist(entries)
        if not delivered:
            # Either the agent went away between the store and the send, or it
            # is a build without the capability. Both are worth naming: the
            # entry is stored and will never be observed until something
            # changes, and an unexplained card that stays grey is the worst
            # version of that.
            detail = (
                "this agent could not be told; it may be disconnected or too old "
                "to watch units and processes"
            )

    return WatchListOut(
        engine_id=engine_id,
        entries=[WatchEntryOut.of(entry) for entry in entries],
        delivered=delivered,
        detail=detail,
    )
