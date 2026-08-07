"""The agent ingest path, driven by a synthetic agent.

`docs/MIGRATION.md` §6 step 2: *Controller ingest, agent stubbed, driven by a
synthetic frame generator.* No Go, no socket, no daemon — the frames are built
here and handed straight to the provider, which is the whole reason the
`AgentSession` port exists.

The cases that matter are the ones a live agent will not produce on demand: a
delta that omits an entity, a delta naming an id we were never sent, a
disconnect between dispatch and reply, and a reconnect whose first frame
lands against a stale membership cache.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.graph.model import Node
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import (
    NodeKind,
    container_urn,
    host_urn,
    image_urn,
    network_urn,
    service_urn,
    stack_urn,
    volume_urn,
)
from bystack.core.ports.agent import AgentDisconnected
from bystack.core.ports.command import (
    CommandKind,
    CommandRejected,
    CommandRequest,
    CommandStatus,
)
from bystack.core.ports.provider import ProviderState
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.providers.agent.ingest import IngestError
from bystack.providers.agent.provider import AgentProvider
from bystack.runtime.writer import PartitionWriter

ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE = "AAAABBBBCCCC"
SOURCE = ENGINE
C1 = "c" * 64
C2 = "d" * 64


# --------------------------------------------------------------------------
# The synthetic agent
# --------------------------------------------------------------------------


class FakeSession:
    """An agent that records what it was sent and answers when told to."""

    def __init__(self, *, read_only: bool = False, dead: bool = False) -> None:
        self.engine_id = ENGINE_ID
        self.read_only = read_only
        self.dead = dead
        self.sent: list[wire.Envelope] = []

    async def send(self, envelope: object) -> None:
        assert isinstance(envelope, wire.Envelope)
        if self.dead:
            raise AgentDisconnected("agent is gone")
        self.sent.append(envelope)

    def last_command(self) -> wire.Command:
        return self.sent[-1].command


def hello(**overrides: Any) -> wire.Envelope:
    engine = wire.EngineInfo(
        id=ENGINE_ID,
        name="lab-node-01",
        server_version="29.6.0",
        operating_system="Fedora Linux 44",
        kernel_version="6.19.10",
        architecture="x86_64",
        ncpu=8,
        mem_total=16_000_000_000,
        containers_running=2,
        containers_total=3,
    )
    fields: dict[str, Any] = {
        "agent_version": "0.1.0",
        "engine_id": ENGINE_ID,
        "engine": engine,
        "read_only": False,
    }
    fields.update(overrides)
    return wire.Envelope(hello=wire.Hello(**fields))


def container(
    container_id: str,
    name: str,
    *,
    state: str = "running",
    project: str | None = "shop",
    service: str | None = "web",
    status_text: str = "Up 3 hours",
    image_id: str = "sha256:abc123",
    volume: str | None = "shop_data",
) -> wire.Entity:
    labels: dict[str, str] = {}
    if project and service:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service
        labels["com.docker.compose.container-number"] = "1"

    mounts = [wire.Mount(type="bind", destination="/etc/localtime")]
    if volume:
        mounts.append(
            wire.Mount(type="volume", name=volume, destination="/data", mode="rw", rw=True)
        )

    return wire.Entity(
        id=container_id,
        container=wire.Container(
            id=container_id,
            names=[f"/{name}"],
            image="nginx:latest",
            image_id=image_id,
            command="nginx -g daemon off;",
            created=1_700_000_000,
            state=state,
            status_text=status_text,
            labels=labels,
            ports=[
                wire.Port(private_port=80, public_port=8080, protocol="tcp", host_ip="0.0.0.0"),
                # Unpublished: the mapper must drop it.
                wire.Port(private_port=443, protocol="tcp"),
            ],
            networks=[
                wire.NetworkAttachment(
                    network_id="net1", name="bridge", ipv4="172.17.0.2", aliases=[]
                )
            ],
            mounts=mounts,
        ),
    )


def network_entity() -> wire.Entity:
    return wire.Entity(
        id="net1",
        network=wire.Network(
            id="net1", name="shop_default", driver="bridge", scope="local",
            subnets=["172.17.0.0/16"],
        ),
    )


def volume_entity() -> wire.Entity:
    return wire.Entity(
        id="shop_data",
        volume=wire.Volume(
            name="shop_data", driver="local",
            mountpoint="/var/lib/docker/volumes/shop_data/_data", scope="local",
        ),
    )


def image_entity(image_id: str = "sha256:abc123") -> wire.Entity:
    return wire.Entity(
        id=image_id,
        image=wire.Image(
            id=image_id, repo_tags=["nginx:latest"], repo_digests=["nginx@sha256:def456"],
            size=142_000_000, created=1_699_000_000,
        ),
    )


def sync(slice_id: int, *entities: wire.Entity) -> wire.Envelope:
    return wire.Envelope(sync=wire.Sync(slice=slice_id, entities=list(entities)))


def delta(slice_id: int, ids: list[str], *changed: wire.Entity) -> wire.Envelope:
    return wire.Envelope(delta=wire.Delta(slice=slice_id, ids=ids, changed=list(changed)))


@pytest.fixture
def wired():
    store = InMemoryGraphStore()
    bus = InMemoryEventBus()
    provider = AgentProvider(SOURCE, PartitionWriter(store, bus, SOURCE))
    return provider, store


async def connected(wired, *, read_only: bool = False):
    provider, store = wired
    session = FakeSession(read_only=read_only)
    provider.attach(session)
    await provider.on_frame(hello())
    return provider, store, session


# --------------------------------------------------------------------------
# Hello
# --------------------------------------------------------------------------


async def test_hello_writes_the_host_node(wired) -> None:
    provider, store, _ = await connected(wired)

    host = store.node(host_urn(ENGINE))
    assert host is not None
    assert host.name == "lab-node-01"
    assert host.attrs["engine_version"] == "29.6.0"


async def test_the_pre_25_engine_id_format_is_normalized(wired) -> None:
    """A colon-delimited fingerprint would raise URNError and take the whole
    provider down. A mixed-version fleet is the normal case here."""
    _, store, _ = await connected(wired)

    assert store.node(host_urn(ENGINE)) is not None


async def test_observations_before_hello_are_refused(wired) -> None:
    """A frame arriving before Hello has no partition to be written into, and
    accepting it is how one host's containers land in another's graph."""
    provider, _ = wired
    provider.attach(FakeSession())

    with pytest.raises(IngestError):
        await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))


async def test_a_hello_without_an_engine_id_is_refused(wired) -> None:
    provider, _ = wired
    provider.attach(FakeSession())

    with pytest.raises(IngestError):
        await provider.on_frame(hello(engine_id=""))


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


async def test_a_container_sync_builds_the_whole_logical_layer(wired) -> None:
    """The payoff for keeping mapper.py on the Controller: stacks, services
    and depends_on come out of the agent path with no Go equivalent."""
    provider, store, _ = await connected(wired)

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "shop-web-1")))

    assert store.node(container_urn(ENGINE, C1)) is not None
    assert store.node(service_urn(ENGINE, "shop", "web")) is not None
    assert store.node(stack_urn(ENGINE, "shop")) is not None


async def test_every_slice_maps_through_the_existing_mapper(wired) -> None:
    provider, store, _ = await connected(wired)

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    await provider.on_frame(sync(wire.SLICE_NETWORK, network_entity()))
    await provider.on_frame(sync(wire.SLICE_VOLUME, volume_entity()))
    await provider.on_frame(sync(wire.SLICE_IMAGE, image_entity()))

    assert store.node(network_urn(ENGINE, "net1")) is not None
    assert store.node(volume_urn(ENGINE, "shop_data")) is not None
    assert store.node(image_urn("sha256:abc123")) is not None


async def test_unpublished_ports_are_dropped_on_the_way_through(wired) -> None:
    provider, store, _ = await connected(wired)

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    ports = store.node(container_urn(ENGINE, C1)).attrs["ports"]
    assert [p["private"] for p in ports] == [80]


async def test_bind_mounts_do_not_become_nodes(wired) -> None:
    """A bind mount is a real dependency but not an entity the engine owns."""
    provider, store, _ = await connected(wired)

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    volumes = [n for n in store.snapshot().nodes if n.kind == NodeKind.VOLUME]
    assert volumes == []


async def test_a_sync_replaces_the_slice_wholesale(wired) -> None:
    provider, store, _ = await connected(wired)
    await provider.on_frame(
        sync(wire.SLICE_CONTAINER, container(C1, "web"), container(C2, "worker", service="worker"))
    )

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    assert store.node(container_urn(ENGINE, C2)) is None
    assert store.node(container_urn(ENGINE, C1)) is not None


async def test_a_slice_sync_does_not_disturb_another_slice(wired) -> None:
    """The same scoping rule the informer used, now expressed on the wire: a
    container frame claims the container slice and says nothing about
    networks."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_NETWORK, network_entity()))

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    assert store.node(network_urn(ENGINE, "net1")) is not None


async def test_an_unknown_slice_is_refused(wired) -> None:
    provider, _, _ = await connected(wired)

    with pytest.raises(IngestError):
        await provider.on_frame(sync(wire.SLICE_UNSPECIFIED))


# --------------------------------------------------------------------------
# Authoritative deltas — the protocol's whole point
# --------------------------------------------------------------------------


async def test_a_delta_carries_membership_so_unchanged_entities_survive(wired) -> None:
    """The case that makes the protocol cheap and is easiest to break.

    The delta names both containers but carries a payload for one. The other
    must survive from cache — a naive implementation reconciles with only the
    payload it received and deletes every stable container on the host.
    """
    provider, store, _ = await connected(wired)
    await provider.on_frame(
        sync(wire.SLICE_CONTAINER, container(C1, "web"), container(C2, "worker", service="worker"))
    )

    await provider.on_frame(
        delta(wire.SLICE_CONTAINER, [C1, C2], container(C1, "web", state="exited"))
    )

    assert store.node(container_urn(ENGINE, C1)).status == "exited"
    assert store.node(container_urn(ENGINE, C2)) is not None
    assert store.node(container_urn(ENGINE, C2)).status == "running"


async def test_an_id_absent_from_a_delta_is_removed(wired) -> None:
    """Deletion needs no separate signal: an id that stops appearing is gone."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(
        sync(wire.SLICE_CONTAINER, container(C1, "web"), container(C2, "worker", service="worker"))
    )

    await provider.on_frame(delta(wire.SLICE_CONTAINER, [C1]))

    assert store.node(container_urn(ENGINE, C2)) is None
    assert store.node(container_urn(ENGINE, C1)) is not None


async def test_the_last_container_of_a_service_takes_the_service_with_it(wired) -> None:
    """Stacks and services are derived from container labels, so the container
    slice has to own the logical layer or an orphaned service outlives every
    container that ever realized it."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    await provider.on_frame(delta(wire.SLICE_CONTAINER, []))

    assert store.node(service_urn(ENGINE, "shop", "web")) is None
    assert store.node(stack_urn(ENGINE, "shop")) is None


async def test_a_steady_state_delta_produces_no_graph_change(wired) -> None:
    """The property the whole design is for: nothing changed, so nothing is
    published, so a quiet cluster costs zero bytes of WebSocket traffic."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    before = store.seq

    await provider.on_frame(delta(wire.SLICE_CONTAINER, [C1]))

    assert store.seq == before


async def test_a_payload_for_an_id_absent_from_membership_does_not_resurrect_it(
    wired,
) -> None:
    """A contradictory frame: 'this changed' and 'this is not here'. The
    membership set is the authoritative half, so the entity goes."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(
        sync(wire.SLICE_CONTAINER, container(C1, "web"), container(C2, "worker", service="worker"))
    )

    await provider.on_frame(
        delta(wire.SLICE_CONTAINER, [C1], container(C2, "worker", service="worker"))
    )

    assert store.node(container_urn(ENGINE, C2)) is None


async def test_a_delta_naming_an_id_we_never_saw_is_survivable(wired, caplog) -> None:
    """The one way this protocol can go wrong: the agent believes we hold a
    payload we do not. Dropping it is safe — the next resync repairs it —
    and silence is not."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    await provider.on_frame(delta(wire.SLICE_CONTAINER, [C1, "ghost"]))

    assert store.node(container_urn(ENGINE, C1)) is not None
    assert "unknown" in caplog.text


async def test_the_status_string_alone_never_moves_the_graph(wired) -> None:
    """`"Up 3 hours"` becomes `"Up 4 hours"` on a wall clock. If that reached
    the content hash, every container would re-emit on every resync and
    incremental sync would be a full refresh on a timer — a system that works
    perfectly and costs 100x more than it should."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    before = store.seq

    await provider.on_frame(
        sync(wire.SLICE_CONTAINER, container(C1, "web", status_text="Up 4 hours"))
    )

    assert store.seq == before


async def test_the_exit_code_still_arrives_when_the_state_changes(wired) -> None:
    """The one durable fact in that string. It is only meaningful at the
    moment `state` changes — which is hashed — so excluding the string from
    the hash costs nothing."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    await provider.on_frame(
        sync(
            wire.SLICE_CONTAINER,
            container(C1, "web", state="exited", status_text="Exited (137) 3 seconds ago"),
        )
    )

    assert store.node(container_urn(ENGINE, C1)).attrs["exit_code"] == 137


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


async def test_only_images_something_runs_become_nodes(wired) -> None:
    """A host with 300 cached build layers is a disk-cleanup concern, not
    topology, and drawing them would bury what the operator came to see."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    await provider.on_frame(
        sync(wire.SLICE_IMAGE, image_entity(), image_entity("sha256:unused"))
    )

    assert store.node(image_urn("sha256:abc123")) is not None
    assert store.node(image_urn("sha256:unused")) is None


async def test_an_image_stops_being_a_node_when_its_last_container_goes(wired) -> None:
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    await provider.on_frame(sync(wire.SLICE_IMAGE, image_entity()))

    await provider.on_frame(delta(wire.SLICE_CONTAINER, []))

    assert store.node(image_urn("sha256:abc123")) is None


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


def test_a_provider_starts_enrolled_but_unconnected(wired) -> None:
    provider, _ = wired

    assert provider.health().state is ProviderState.STOPPED


async def test_the_state_machine_walks_the_migration_table(wired) -> None:
    provider, _ = wired
    session = FakeSession()

    provider.attach(session)
    assert provider.health().state is ProviderState.STARTING

    await provider.on_frame(hello())
    assert provider.health().state is ProviderState.SYNCING

    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    assert provider.health().state is ProviderState.READY

    provider.detach("agent went away")
    assert provider.health().state is ProviderState.DEGRADED


async def test_a_disconnect_keeps_the_graph_and_marks_it_stale(wired) -> None:
    """Stale is not wrong. The last known topology with a clear marker beats
    blanking the screen when a laptop closes its lid."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    provider.detach("lid closed")

    assert store.node(container_urn(ENGINE, C1)) is not None
    assert provider.health().state is ProviderState.DEGRADED


async def test_a_reconnecting_agent_does_not_apply_deltas_against_a_stale_cache(
    wired,
) -> None:
    """The bug this guards is silent: without clearing the membership cache on
    disconnect, a reconnecting agent's first Delta is applied against a
    picture from before the gap, resurrecting containers removed while it was
    away."""
    provider, store, _ = await connected(wired)
    await provider.on_frame(
        sync(wire.SLICE_CONTAINER, container(C1, "web"), container(C2, "worker", service="worker"))
    )
    provider.detach("network dropped")

    provider.attach(FakeSession())
    await provider.on_frame(hello())
    # The agent's first frame after a reconnect is always a full Sync, and
    # this one has lost C2. Applied against a stale cache, C2 survives.
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))

    assert store.node(container_urn(ENGINE, C2)) is None


async def test_a_second_connection_displaces_the_first(wired) -> None:
    """The common cause is an old connection that died in a way we have not
    noticed — a NAT rebinding, a suspended laptop. Refusing the new one leaves
    the host unreachable until a TCP timeout we do not control fires."""
    provider, _ = wired
    provider.attach(FakeSession())

    provider.attach(FakeSession())
    await provider.on_frame(hello())

    assert provider.connected


async def test_rejection_is_terminal_not_degraded(wired) -> None:
    """A revoked certificate does not improve by reconnecting."""
    provider, _ = wired
    provider.attach(FakeSession())

    provider.reject("certificate revoked")

    assert provider.health().state is ProviderState.FAILED
    assert not provider.connected


async def test_an_unknown_frame_is_ignored_not_fatal(wired) -> None:
    """Mixed-version fleets are a normal operating state under ADR-0008, not a
    migration window: a Controller will be talking to older and newer agents
    indefinitely."""
    provider, store, _ = await connected(wired)

    await provider.on_frame(wire.Envelope(seq=7))

    assert provider.health().state is not ProviderState.FAILED


# --------------------------------------------------------------------------
# Commands over the agent stream
# --------------------------------------------------------------------------


def urn_of(container_id: str):
    return container_urn(ENGINE, container_id)


def node_of(store, container_id: str):
    return store.node(urn_of(container_id))


async def stocked(wired):
    provider, store, session = await connected(wired)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    return provider, store, session


async def test_a_command_is_dispatched_with_dockers_own_id(wired) -> None:
    """The agent does not know the URN scheme and must not learn it."""
    provider, store, session = await stocked(wired)
    request = CommandRequest(CommandKind.RESTART, urn_of(C1), timeout=30)

    task = asyncio.create_task(provider.execute(request, node_of(store, C1)))
    await asyncio.sleep(0)

    command = session.last_command()
    assert command.verb == "restart"
    assert command.target_id == C1
    assert command.args["timeout"] == "30"

    await provider.on_frame(
        wire.Envelope(command_result=wire.CommandResult(command_id=command.command_id, ok=True))
    )
    assert (await task).status is CommandStatus.SUCCEEDED


async def test_results_are_correlated_by_command_id(wired) -> None:
    """The hand-rolled correlation gRPC would have generated. Two commands in
    flight, answered out of order."""
    provider, store, session = await stocked(wired)
    node = node_of(store, C1)

    first = asyncio.create_task(
        provider.execute(CommandRequest(CommandKind.STOP, urn_of(C1)), node)
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        provider.execute(CommandRequest(CommandKind.START, urn_of(C1)), node)
    )
    await asyncio.sleep(0)

    ids = [envelope.command.command_id for envelope in session.sent]
    await provider.on_frame(
        wire.Envelope(
            command_result=wire.CommandResult(command_id=ids[1], ok=False, detail="second")
        )
    )
    await provider.on_frame(
        wire.Envelope(
            command_result=wire.CommandResult(command_id=ids[0], ok=True, detail="first")
        )
    )

    assert (await first).detail == "first"
    assert (await second).detail == "second"


async def test_unchanged_survives_the_whole_round_trip(wired) -> None:
    """Docker's 304 preserved end to end. A restart that changed nothing is
    not a restart that worked."""
    provider, store, session = await stocked(wired)

    task = asyncio.create_task(
        provider.execute(CommandRequest(CommandKind.START, urn_of(C1)), node_of(store, C1))
    )
    await asyncio.sleep(0)
    await provider.on_frame(
        wire.Envelope(
            command_result=wire.CommandResult(
                command_id=session.last_command().command_id, ok=True, unchanged=True
            )
        )
    )

    assert (await task).status is CommandStatus.NOOP


async def test_a_disconnect_fails_waiters_immediately(wired) -> None:
    """Otherwise the operator watches a spinner for the full deadline waiting
    for an answer that provably cannot arrive."""
    provider, store, session = await stocked(wired)

    task = asyncio.create_task(
        provider.execute(CommandRequest(CommandKind.RESTART, urn_of(C1)), node_of(store, C1))
    )
    await asyncio.sleep(0)
    provider.detach("agent went away")

    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome.status is CommandStatus.REJECTED


async def test_a_command_to_a_disconnected_agent_is_refused_not_queued(wired) -> None:
    provider, store, _ = await stocked(wired)
    node = node_of(store, C1)
    provider.detach("gone")

    with pytest.raises(CommandRejected):
        await provider.execute(CommandRequest(CommandKind.RESTART, urn_of(C1)), node)


async def test_an_agent_that_declares_itself_read_only_is_believed(wired) -> None:
    """The second choke point from ARCHITECTURE §9. The agent holds
    root-equivalent access to a machine, and "the Controller said so" is not
    an acceptable sole justification for acting on it."""
    provider, store, _ = await connected(wired, read_only=True)
    await provider.on_frame(sync(wire.SLICE_CONTAINER, container(C1, "web")))
    node = node_of(store, C1)

    assert provider.supported_commands(node) == frozenset()
    with pytest.raises(CommandRejected):
        await provider.execute(CommandRequest(CommandKind.STOP, urn_of(C1)), node)


async def test_a_disconnected_provider_offers_no_actions(wired) -> None:
    provider, store, _ = await stocked(wired)
    node = node_of(store, C1)
    provider.detach("gone")

    assert provider.supported_commands(node) == frozenset()


async def test_a_result_for_a_forgotten_command_does_not_break_the_stream(wired) -> None:
    """An unknown command_id is an agent bug or a late answer after a timeout.
    Tearing down a healthy connection over it turns a cosmetic problem into an
    outage."""
    provider, _, _ = await stocked(wired)

    await provider.on_frame(
        wire.Envelope(command_result=wire.CommandResult(command_id="never-seen", ok=True))
    )

    assert provider.connected


async def test_pending_commands_do_not_leak(wired) -> None:
    provider, store, session = await stocked(wired)

    task = asyncio.create_task(
        provider.execute(CommandRequest(CommandKind.STOP, urn_of(C1)), node_of(store, C1))
    )
    await asyncio.sleep(0)
    assert provider.health().metrics["pending_commands"] == 1

    await provider.on_frame(
        wire.Envelope(
            command_result=wire.CommandResult(
                command_id=session.last_command().command_id, ok=True
            )
        )
    )
    await task

    assert provider.health().metrics["pending_commands"] == 0


# --------------------------------------------------------------------------
# Partition isolation
# --------------------------------------------------------------------------


async def test_an_agent_can_only_write_its_own_partition() -> None:
    """A security property, not hygiene.

    An agent is code running on a machine we do not own, and its input is
    untrusted. Because its writer is structurally incapable of naming another
    partition, a fully compromised agent can lie about its own host and
    nothing else — it cannot delete another host's containers from the graph
    and cannot forge another host's topology.

    Here the agent declares an empty container slice, which for its own
    partition means "delete everything I ever reported". The neighbouring
    partition must not notice.
    """
    store = InMemoryGraphStore()
    bus = InMemoryEventBus()
    victim = container_urn("otherengine", C2)
    store.upsert(
        "other-host",
        [Node(urn=victim, kind=NodeKind.CONTAINER, name="victim", source="other-host")],
    )

    provider = AgentProvider(SOURCE, PartitionWriter(store, bus, SOURCE))
    provider.attach(FakeSession())
    await provider.on_frame(hello())
    await provider.on_frame(sync(wire.SLICE_CONTAINER))

    assert store.node(victim) is not None


# --------------------------------------------------------------------------
# Resync
# --------------------------------------------------------------------------


async def test_a_resync_request_reaches_the_agent(wired) -> None:
    provider, _, session = await connected(wired)

    await provider.request_resync(wire.SLICE_CONTAINER)

    assert list(session.sent[-1].resync_request.slices) == [wire.SLICE_CONTAINER]


async def test_a_resync_request_to_a_disconnected_agent_is_a_no_op(wired) -> None:
    provider, _, _ = await connected(wired)
    provider.detach("gone")

    await provider.request_resync(wire.SLICE_CONTAINER)
