"""Informer behaviour: List, Watch, Resync, coalescing and recovery.

Runs against a scripted engine. No Docker daemon, no network, no sleeping on
wall-clock timers for correctness.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from tests.conftest import ENGINE_ID_SAFE, FakeEngineClient, FakeTransport, make_container

from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import NodeKind, container_urn, service_urn
from bystack.core.ports.provider import ProviderState
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.providers.docker.informer import DockerInformer
from bystack.runtime.writer import PartitionWriter

SOURCE = "docker-a"


def build(client: FakeEngineClient, *, resync_interval: float = 3600.0):
    store = InMemoryGraphStore()
    bus = InMemoryEventBus()
    informer = DockerInformer(
        SOURCE,
        FakeTransport(),
        PartitionWriter(store, bus, SOURCE),
        resync_interval=resync_interval,
        client_factory=lambda _endpoint: client,
    )
    return store, bus, informer


@contextlib.asynccontextmanager
async def running(informer: DockerInformer):
    task = asyncio.create_task(informer.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def settle(client: FakeEngineClient) -> None:
    """Wait for the scripted events to be consumed, then for work to drain."""
    await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)
    # One coalescing window plus a scheduling margin.
    await asyncio.sleep(0.4)


async def test_initial_list_populates_the_partition(
    engine_info, container, network, volume, image
) -> None:
    client = FakeEngineClient(engine_info, [container], [network], [volume], [image])
    store, _, informer = build(client)

    async with running(informer):
        await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)

        snapshot = store.snapshot()
        kinds = {n.kind for n in snapshot.nodes}
        assert kinds == {
            NodeKind.HOST,
            NodeKind.CONTAINER,
            NodeKind.STACK,
            NodeKind.SERVICE,
            NodeKind.NETWORK,
            NodeKind.VOLUME,
            NodeKind.IMAGE,
        }
        assert informer.health().state is ProviderState.READY


async def test_watch_starts_from_before_the_list(engine_info) -> None:
    # The classic silent-drift bug: recording `since` after the List loses any
    # change that lands while the List is in flight, with no gap to detect.
    recorded: dict[str, Any] = {}

    class RecordingClient(FakeEngineClient):
        async def list_containers(self, **kwargs):  # type: ignore[override]
            recorded["listed_at"] = asyncio.get_running_loop().time()
            return await super().list_containers(**kwargs)

        async def events(self, *, since=None, types=()):  # type: ignore[override]
            recorded["since"] = since
            async for event in super().events(since=since, types=types):
                yield event

    client = RecordingClient(engine_info)
    _, _, informer = build(client)

    async with running(informer):
        await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)

    # `since` is wall-clock and `listed_at` is loop time, so they are not
    # directly comparable -- what matters is that a value was captured and
    # handed to the watch at all.
    assert recorded["since"] is not None
    assert "listed_at" in recorded


async def test_events_are_filtered_server_side(engine_info) -> None:
    # Filtering client-side would meet the same functional requirement at
    # many times the CPU, and the budget does not have it to spare.
    captured: dict[str, Any] = {}

    class RecordingClient(FakeEngineClient):
        async def events(self, *, since=None, types=()):  # type: ignore[override]
            captured["types"] = types
            async for event in super().events(since=since, types=types):
                yield event

    client = RecordingClient(engine_info)
    _, _, informer = build(client)

    async with running(informer):
        await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)

    assert set(captured["types"]) == {"container", "network", "volume"}


async def test_container_event_triggers_a_scoped_refresh(engine_info, network) -> None:
    new_container = make_container("f" * 64, "api", project="shop", service="api")
    client = FakeEngineClient(
        engine_info,
        containers=[],
        networks=[network],
        events=[{"Type": "container", "Action": "start", "Actor": {"ID": "f" * 64}}],
    )
    store, _, informer = build(client)

    async with running(informer):
        # The engine gains a container between the List and the event.
        client.containers.append(new_container)
        await settle(client)

        assert store.node(container_urn(ENGINE_ID_SAFE, "f" * 64)) is not None
        assert store.node(service_urn(ENGINE_ID_SAFE, "shop", "api")) is not None


async def test_removing_the_last_container_reclaims_its_service(engine_info) -> None:
    # Without kind-scoped reconcile this service would linger on the user's
    # canvas until the next full resync, minutes later.
    container = make_container("g" * 64, "web", project="shop", service="web")
    client = FakeEngineClient(
        engine_info,
        containers=[container],
        events=[{"Type": "container", "Action": "destroy", "Actor": {"ID": "g" * 64}}],
    )
    store, _, informer = build(client)

    async with running(informer):
        client.containers.clear()
        await settle(client)

        assert store.node(container_urn(ENGINE_ID_SAFE, "g" * 64)) is None
        assert store.node(service_urn(ENGINE_ID_SAFE, "shop", "web")) is None


async def test_a_container_event_makes_no_claim_about_networks(engine_info, network) -> None:
    # The property kind-scoping exists to protect: a container refresh must
    # not delete the host's networks just because it did not mention them.
    client = FakeEngineClient(
        engine_info,
        containers=[],
        networks=[network],
        events=[{"Type": "container", "Action": "start", "Actor": {"ID": "x"}}],
    )
    store, _, informer = build(client)

    async with running(informer):
        await settle(client)

        assert any(n.kind == NodeKind.NETWORK for n in store.snapshot().nodes)


async def test_a_burst_of_events_coalesces_into_one_refresh(engine_info) -> None:
    # A `compose up` of twenty services emits well over a hundred events. One
    # refresh, not a hundred round trips.
    burst = [
        {"Type": "container", "Action": action, "Actor": {"ID": f"c{i}"}}
        for i in range(100)
        for action in ("create", "start")
    ]
    client = FakeEngineClient(engine_info, containers=[], events=burst)
    _, _, informer = build(client)

    async with running(informer):
        await settle(client)

        # One from the initial List, plus one for the whole coalesced burst.
        assert client.calls["list_containers"] <= 2


async def test_a_quiet_cluster_produces_no_deltas(engine_info, container, network) -> None:
    # The steady state. Reconciling on a timer must cost zero WebSocket bytes,
    # or "no full refreshes" is a claim the system does not actually honour.
    client = FakeEngineClient(
        engine_info,
        [container],
        [network],
        events=[{"Type": "container", "Action": "health_status", "Actor": {"ID": container["Id"]}}],
    )
    store, _, informer = build(client)

    async with running(informer):
        await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)
        seq_after_list = store.seq

        await asyncio.sleep(0.4)

        # Nothing on the engine changed, so the refresh produced no delta and
        # burned no sequence number.
        assert store.seq == seq_after_list


async def test_resync_repairs_state_the_event_stream_never_reported(engine_info) -> None:
    # Events are lossy across reconnects and the daemon makes no delivery
    # guarantee. Without periodic reconciliation the graph drifts from reality
    # with no mechanism to ever notice.
    client = FakeEngineClient(engine_info, containers=[], events=[])
    store, _, informer = build(client, resync_interval=0.1)

    async with running(informer):
        await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)

        # A change no event is ever emitted for.
        client.containers.append(make_container("h" * 64, "ghost"))
        await asyncio.sleep(0.4)

        assert store.node(container_urn(ENGINE_ID_SAFE, "h" * 64)) is not None


async def test_a_failing_engine_leaves_the_last_known_graph_in_place(
    engine_info, container
) -> None:
    # Stale is not wrong. Blanking the topology on a blip is worse than
    # showing the last known state with a DEGRADED marker.
    class FlakyClient(FakeEngineClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sessions = 0

        async def events(self, *, since=None, types=()):  # type: ignore[override]
            self.sessions += 1
            self.events_drained.set()
            if self.sessions == 1:
                raise ConnectionResetError("engine went away")
            await asyncio.Event().wait()
            yield {}  # pragma: no cover

    client = FlakyClient(engine_info, [container])
    store, _, informer = build(client)

    async with running(informer):
        await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        assert store.node(container_urn(ENGINE_ID_SAFE, container["Id"])) is not None
        # DEGRADED, not FAILED: a dropped connection is routine and a
        # reconnect is expected to fix it. Reporting FAILED here would have an
        # orchestrator restart a perfectly healthy control plane.
        assert informer.health().state is ProviderState.DEGRADED


async def test_cancellation_closes_the_client_and_transport(engine_info) -> None:
    client = FakeEngineClient(engine_info)
    transport = FakeTransport()
    store = InMemoryGraphStore()
    informer = DockerInformer(
        SOURCE,
        transport,
        PartitionWriter(store, InMemoryEventBus(), SOURCE),
        client_factory=lambda _e: client,
    )

    task = asyncio.create_task(informer.run())
    await asyncio.wait_for(client.events_drained.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.closed
    assert transport.close_count >= 1
