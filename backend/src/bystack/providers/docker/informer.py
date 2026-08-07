"""Docker informer: List -> Watch -> Resync.

The pattern is taken from Kubernetes client-go, because it is the proven
answer to "real-time without polling" and there is no reason to invent a
worse one.

::

    t0  record `since`
    t1  List      -> full reconcile of the partition   (authoritative)
    t2  Watch     -> GET /events?since=t0              (no gap: t0 < t1)
    ..  Resync    -> re-List every N minutes           (repairs dropped events)

**Why events trigger a re-List instead of carrying data.** A Docker event
tells us *that* something changed and gives us an id. We could inspect that
id -- but then we would also have to reason about what became orphaned (the
service whose last container just died), what edges went stale, and what
happens when two events arrive out of order. Instead an event triggers a
kind-scoped reconcile of the affected slice: "here are all my containers
now". One HTTP call, always correct, no ordering assumptions.

That sounds expensive and is not, for two reasons. Bursts are coalesced, so a
``compose up`` of twenty services costs one or two calls rather than twenty.
And the *emitted delta* is still incremental -- the store compares content
hashes and publishes only what genuinely changed, so a re-List that finds
nothing new produces zero bytes of WebSocket traffic.

At the stated target (3-10 hosts) this is comfortably the right trade. A host
with tens of thousands of containers would want per-id inspection instead;
that would be a change confined to this file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Final

from bystack.core.identity import NodeKind, engine_scope
from bystack.core.ports.provider import GraphWriter, ProviderHealth, ProviderState
from bystack.core.ports.transport import ChannelEndpoint, Transport, TransportError
from bystack.providers.docker.client import EngineClient, EngineError
from bystack.providers.docker.mapper import (
    build_container_slice,
    build_partition,
    map_network,
    map_volume,
)

log = logging.getLogger(__name__)

#: Server-side event filter. Everything outside these types (exec_start,
#: exec_die, daemon reload, plugin churn) is discarded by the daemon and never
#: reaches our process, our CPU, or our JSON parser.
WATCHED_TYPES: Final = ("container", "network", "volume")

#: Which slice of the partition each event type invalidates.
#:
#: A container event invalidates the whole logical layer as well, because
#: stacks and services are *derived* from container labels -- the last
#: container of a service disappearing is what makes that service cease to
#: exist.
_SCOPES: Final[dict[str, frozenset[str]]] = {
    "container": frozenset({NodeKind.CONTAINER, NodeKind.STACK, NodeKind.SERVICE}),
    "network": frozenset({NodeKind.NETWORK}),
    "volume": frozenset({NodeKind.VOLUME}),
}

#: Burst coalescing window. Long enough to collapse a `compose up`, short
#: enough to stay imperceptible in the UI.
COALESCE_WINDOW: Final = 0.25

#: Reconnect backoff bounds. A host that is down must not be retried in a
#: hot loop -- that is how a control plane becomes the outage.
_BACKOFF_MIN: Final = 1.0
_BACKOFF_MAX: Final = 60.0


#: Losing a connection to a managed host is routine, not a defect. These are
#: the failures a reconnect is expected to fix.
_RECOVERABLE: Final = (EngineError, TransportError, OSError)


def _is_recoverable(exc: BaseException) -> bool:
    """Unwrap exception groups before classifying.

    A group counts as recoverable only if every leaf is: one genuinely
    unexpected failure inside a group is still an unexpected failure, and
    hiding it behind a sibling connection error is how real bugs get logged
    as "host unreachable" forever.
    """
    if isinstance(exc, BaseExceptionGroup):
        return all(_is_recoverable(leaf) for leaf in exc.exceptions)
    return isinstance(exc, _RECOVERABLE)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(leaf) for leaf in exc.exceptions)
    return str(exc) or repr(exc)


class DockerInformer:
    """Keeps one engine's graph partition current."""

    __slots__ = (
        "_source",
        "_transport",
        "_writer",
        "_resync_interval",
        "_client_factory",
        "_client",
        "_engine_id",
        "_dirty",
        "_health",
        "_last_sync_at",
        "_node_count",
        "_full_syncs",
        "_incremental_syncs",
    )

    def __init__(
        self,
        source: str,
        transport: Transport,
        writer: GraphWriter,
        *,
        resync_interval: float = 300.0,
        client_factory: Callable[[ChannelEndpoint], EngineClient] = EngineClient,
    ) -> None:
        self._source = source
        self._transport = transport
        self._writer = writer
        self._resync_interval = resync_interval
        # Injected so the List/Watch/Resync logic -- by far the subtlest code
        # in the provider -- is testable against a scripted engine, with no
        # Docker daemon and no network anywhere in the test suite.
        self._client_factory = client_factory
        self._client: EngineClient | None = None
        self._engine_id: str | None = None
        self._dirty: asyncio.Queue[str] = asyncio.Queue()
        self._health = ProviderHealth(state=ProviderState.STOPPED)
        self._last_sync_at = 0.0
        self._node_count = 0
        self._full_syncs = 0
        self._incremental_syncs = 0

    @property
    def client(self) -> EngineClient | None:
        """The live engine client, or ``None`` while disconnected.

        Commands borrow the informer's connection rather than opening their
        own. Opening a second one would mean a second transport -- a second
        SSH tunnel per host, established at the moment an operator is already
        waiting -- to reach a daemon we are demonstrably already talking to.

        ``None`` is the honest answer during a reconnect, and the command
        layer turns it into a clean rejection. That is strictly better than
        dialling out mid-incident and timing out slowly.
        """
        return self._client

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            state=self._health.state,
            detail=self._health.detail,
            last_sync_at=self._last_sync_at,
            node_count=self._node_count,
            metrics={
                "full_syncs": self._full_syncs,
                "incremental_syncs": self._incremental_syncs,
                "transport": self._transport.health().state,
            },
        )

    # -- supervision ------------------------------------------------------

    async def run(self) -> None:
        """Run until cancelled, reconnecting with exponential backoff."""
        backoff = _BACKOFF_MIN
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                self._set_state(ProviderState.STOPPED)
                raise
            except Exception as exc:
                # `_session` runs its tasks in a TaskGroup, which wraps
                # whatever they raise in an ExceptionGroup. Matching on the
                # concrete types alone would therefore never fire, and an
                # ordinary connection drop would be reported as FAILED --
                # a permanently broken provider -- rather than DEGRADED.
                if _is_recoverable(exc):
                    # The graph keeps whatever this provider last observed.
                    # Stale is not wrong: the last known topology with a clear
                    # DEGRADED marker beats blanking the screen on a blip.
                    self._set_state(ProviderState.DEGRADED, _describe(exc))
                    log.warning(
                        "provider %s lost its session: %s (retry in %.0fs)",
                        self._source, _describe(exc), backoff,
                    )
                else:
                    self._set_state(ProviderState.FAILED, repr(exc))
                    log.exception("provider %s failed unexpectedly", self._source)

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
            else:
                backoff = _BACKOFF_MIN

    async def _session(self) -> None:
        """One connected lifetime: List, then Watch, until something breaks."""
        self._set_state(ProviderState.STARTING)
        endpoint = await self._transport.open()
        client = self._client_factory(endpoint)
        self._client = client

        try:
            info = await client.info()
            self._engine_id = engine_scope(info["ID"])

            # Recorded BEFORE the List. The event stream then starts from a
            # point that precedes our snapshot, so a change landing during the
            # List is replayed rather than lost. Ordering this the other way
            # is the classic silent-drift bug in every watch implementation.
            since = time.time()

            self._set_state(ProviderState.SYNCING)
            await self._full_sync(client, info)
            self._set_state(ProviderState.READY)

            async with asyncio.TaskGroup() as group:
                group.create_task(self._watch(client, since))
                group.create_task(self._coalescer(client))
                group.create_task(self._resync_loop(client))
        finally:
            self._client = None
            await client.aclose()
            await self._transport.close()

    # -- List -------------------------------------------------------------

    async def _full_sync(self, client: EngineClient, info: dict[str, Any] | None = None) -> None:
        """Reconcile the entire partition from a complete List."""
        info = info or await client.info()

        # Concurrent because they are independent reads; four sequential
        # round trips over an SSH tunnel is four times the latency for no
        # reason.
        containers, networks, volumes, images = await asyncio.gather(
            client.list_containers(),
            client.list_networks(),
            client.list_volumes(),
            client.list_images(),
        )

        nodes, edges = build_partition(
            self._source, info, containers, networks, volumes, images
        )
        await self._writer.reconcile(nodes, edges)

        self._node_count = len(nodes)
        self._last_sync_at = time.time()
        self._full_syncs += 1

    # -- Watch ------------------------------------------------------------

    async def _watch(self, client: EngineClient, since: float) -> None:
        """Consume the engine event stream and mark slices dirty.

        Deliberately does no graph work itself. Its only job is to be fast
        enough never to become the bottleneck during a burst; the actual
        refresh happens in the coalescer.
        """
        async for event in client.events(since=since, types=WATCHED_TYPES):
            scope = event.get("Type")
            if scope in _SCOPES:
                self._dirty.put_nowait(scope)

        # The stream ended without an exception, which means the daemon closed
        # it. Raising sends us back through the supervisor, which re-Lists --
        # necessary, because we have no idea what happened while disconnected.
        raise EngineError("event stream closed by the daemon")

    async def _coalescer(self, client: EngineClient) -> None:
        """Debounce dirty slices into batched refreshes.

        A ``compose up`` of twenty services emits well over a hundred events
        in a fraction of a second. Without this, that is a hundred round trips
        and a hundred deltas; with it, one refresh and one delta.
        """
        while True:
            scopes = {await self._dirty.get()}

            await asyncio.sleep(COALESCE_WINDOW)
            while not self._dirty.empty():
                scopes.add(self._dirty.get_nowait())

            try:
                await self._refresh(client, scopes)
            except EngineError as exc:
                # Do not tear down the session for one failed refresh; the
                # resync loop will repair it shortly.
                log.warning("provider %s refresh failed: %s", self._source, exc)

    async def _refresh(self, client: EngineClient, scopes: set[str]) -> None:
        """Kind-scoped reconcile of the slices an event burst invalidated."""
        engine_id = self._engine_id
        if engine_id is None:  # pragma: no cover - session guarantees this
            return

        if "container" in scopes:
            containers = await client.list_containers()
            nodes, edges = build_container_slice(self._source, engine_id, containers)
            await self._writer.reconcile(nodes, edges, kinds=_SCOPES["container"])

        if "network" in scopes:
            payloads = await client.list_networks()
            mapped = [map_network(self._source, engine_id, p) for p in payloads]
            await self._writer.reconcile(
                [n for n, _ in mapped], [e for _, e in mapped], kinds=_SCOPES["network"]
            )

        if "volume" in scopes:
            payloads = await client.list_volumes()
            mapped = [map_volume(self._source, engine_id, p) for p in payloads]
            await self._writer.reconcile(
                [n for n, _ in mapped], [e for _, e in mapped], kinds=_SCOPES["volume"]
            )

        self._last_sync_at = time.time()
        self._incremental_syncs += 1

    # -- Resync -----------------------------------------------------------

    async def _resync_loop(self, client: EngineClient) -> None:
        """Periodic full reconcile.

        Not optional. Event streams are lossy across reconnects and the daemon
        does not guarantee delivery, so without this the graph would drift
        from reality with no mechanism to notice. It is also cheap: four reads
        against one host every few minutes.
        """
        while True:
            await asyncio.sleep(self._resync_interval)
            try:
                await self._full_sync(client)
            except EngineError as exc:
                log.warning("provider %s resync failed: %s", self._source, exc)

    # -- health -----------------------------------------------------------

    def _set_state(self, state: ProviderState, detail: str | None = None) -> None:
        self._health = ProviderHealth(state=state, detail=detail)
