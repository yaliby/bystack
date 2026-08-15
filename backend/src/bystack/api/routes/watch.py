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
    POST   /agents/watch                watch this on these hosts.

**The last one is the same operation N times, and nothing more.** It exists
because "watch nginx on all nine of these" is one decision an operator makes
and nine requests a browser would otherwise send, each able to fail on its
own with nowhere to report it. What it produces is nine ordinary entries in
nine ordinary per-host lists — the fan-out does not survive the request, and
afterwards there is no such thing as editing "the group". The only trace is a
shared `group_id`, which is a label saying where these came from, so a panel
can tell an operator they asked for this elsewhere too.

That restraint is deliberate and it is the difference between this and fleet
policy. A group that owned its members would have to be reconciled against
the per-host lists forever, and the first thing that reconciliation does is
put entries on a host nobody selected them for.

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

import uuid
from collections import Counter

from fastapi import APIRouter, HTTPException, Query

from bystack.api.deps import Collectors, Context
from bystack.api.schemas import (
    FanoutHostOut,
    InventoryOut,
    WatchEntryIn,
    WatchEntryOut,
    WatchFanoutIn,
    WatchFanoutOut,
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
    "/watch",
    response_model=list[WatchEntryOut],
    summary="What the whole fleet is watching",
)
async def get_all_watchlists(context: Context) -> list[WatchEntryOut]:
    """Every entry on every host, oldest host first.

    One read rather than one per host, and it exists for a question a single
    host's list cannot answer: *this card is a member of a group — which other
    machines hold it, and what are they called?* The operations bar needs that
    before it can offer to restart a service on more than the machine whose
    card was clicked.

    Cheap, and bounded twice over: `MAX_ENTRIES` per host, and a fleet a
    topology map is legible for. It is also *ours* — no host is touched — so
    unlike the inventory this answers instantly for a fleet that is entirely
    asleep.
    """
    sizes = _group_sizes(context)
    return [
        WatchEntryOut.of(entry, sizes[entry.group_id]) for entry in context.watchlist.all()
    ]


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
    sizes = _group_sizes(context)
    return WatchListOut(
        engine_id=scoped,
        entries=[WatchEntryOut.of(entry, sizes[entry.group_id]) for entry in entries],
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


@router.post(
    "/watch",
    response_model=WatchFanoutOut,
    summary="Watch the same thing on several hosts",
)
async def add_watch_across(
    context: Context, collector: Collectors, body: WatchFanoutIn
) -> WatchFanoutOut:
    """Store the same selection on every host named, under one group id.

    **N independent entries, and one label saying they were chosen together.**
    Nothing is stored about the group itself. Each host gets exactly what
    `add_watch` would have given it, and from the next request onwards there is
    no operation that treats these as a unit — removing one leaves the rest,
    which is the point rather than a shortcoming.

    **One host's refusal is not the request's.** A fan-out across nine hosts
    where the fourth already watches the unit has done the right thing eight
    times, and unwinding those eight would be a worse answer to a duplicate
    than keeping them. So every host is attempted, each outcome is reported in
    its own words, and the status code is 200 for all of them — see
    :class:`~bystack.api.schemas.WatchFanoutOut` on why not 201.

    The order is stable: hosts are answered in the order they were asked for,
    so a UI can put the results beside the checkboxes that produced them.
    """
    group_id = uuid.uuid4().hex[:12]
    # Collapsed rather than refused. Two checkboxes for the same machine is a
    # UI that let it happen, and the second one means nothing the first did
    # not; failing the whole request over it would be pedantry with nine hosts
    # of collateral.
    targets = list(dict.fromkeys(engine_scope(engine_id) for engine_id in body.engine_ids))

    hosts: list[FanoutHostOut] = []
    for engine_id in targets:
        try:
            await context.watchlist.add(
                WatchEntry(
                    id="",
                    engine_id=engine_id,
                    kind=body.kind,
                    group_id=group_id,
                    name=body.name,
                    match_kind=body.match_kind,
                    pattern=body.pattern,
                    label=body.label,
                )
            )
        except WatchRejected as exc:
            # The kernel's messages are written to be shown to whoever typed
            # the thing, and they stay per host here for the same reason: "this
            # host already watches 'nginx.service'" is the answer for that row
            # and not a verdict on the other eight.
            hosts.append(FanoutHostOut(engine_id=engine_id, stored=False, detail=str(exc)))
            continue

        delivered, detail = await _deliver(context, collector, engine_id)
        hosts.append(
            FanoutHostOut(
                engine_id=engine_id, stored=True, delivered=delivered, detail=detail
            )
        )

    return WatchFanoutOut(
        group_id=group_id,
        stored=sum(1 for host in hosts if host.stored),
        hosts=hosts,
    )


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


async def _deliver(
    context: Context, collector: Collectors, engine_id: str
) -> tuple[bool, str | None]:
    """Send the whole list to one host, and say what happened in words.

    Complete rather than incremental, matching the frame: the agent is told
    what to watch, not what changed, so a delivery that goes missing costs a
    stale list rather than an entry that is silently never removed.

    Split out from :func:`_push` for the fan-out, which needs this answer per
    host and does not want the host's whole list back nine times.
    """
    entries = context.watchlist.entries(engine_id)
    provider = collector.agent_provider(engine_id, create=False)

    if provider is None:
        return False, "no agent has ever connected for this host"
    if not provider.connected:
        return False, "this host is not connected; it will be told when it reconnects"
    if await provider.send_watchlist(entries):
        return True, None
    # Either the agent went away between the store and the send, or it is a
    # build without the capability. Both are worth naming: the entry is stored
    # and will never be observed until something changes, and an unexplained
    # card that stays grey is the worst version of that.
    return False, (
        "this agent could not be told; it may be disconnected or too old "
        "to watch units and processes"
    )


async def _push(context: Context, collector: Collectors, engine_id: str) -> WatchListOut:
    """One host's list, sent and then returned as it now stands."""
    delivered, detail = await _deliver(context, collector, engine_id)
    entries = context.watchlist.entries(engine_id)
    sizes = _group_sizes(context)
    return WatchListOut(
        engine_id=engine_id,
        entries=[WatchEntryOut.of(entry, sizes[entry.group_id]) for entry in entries],
        delivered=delivered,
        detail=detail,
    )


def _group_sizes(context: Context) -> Counter[str]:
    """How many hosts each act of selection reached.

    Fleet-wide by necessity: a group's other members live in other hosts'
    partitions, and this is the one question about a watch entry that cannot
    be answered from the list it is in. A whole-store scan for that is
    affordable at the size this store is *bounded* to — `MAX_ENTRIES` per host
    against a fleet a topology map is legible for — and the alternative is an
    index that has to be kept true through every add, every remove and every
    load from disk, to save a walk of a few hundred small objects.

    Hosts rather than entries, so the count answers "how many machines did I
    ask this of" and not "how many rows exist", which are the same number
    today only because the duplicate rule makes them so.
    """
    seen: set[tuple[str, str]] = set()
    sizes: Counter[str] = Counter()
    for entry in context.watchlist.all():
        key = (entry.group_id, entry.engine_id)
        if key in seen:
            continue
        seen.add(key)
        sizes[entry.group_id] += 1
    return sizes
