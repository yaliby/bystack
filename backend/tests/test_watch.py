"""Watched units and processes, end to end on the Controller's side.

The agent is synthetic here, exactly as it is in `test_agent_ingest.py`: the
frames are built in this file and handed to the provider, so every interesting
case is reachable without systemd, without `/proc`, and without a machine that
happens to be running the right daemon.

What is worth checking is mostly *not* the happy path. A unit that is not
installed, a rule that matches nothing, a host that is asleep when its list
changes, an agent too old to watch anything, and a unit name with a slash in it
are the cases that decide whether this feature is honest — and none of them is
a case a live host produces on demand.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.test_agent_ingest import ENGINE, FakeSession, hello

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.graph.model import EdgeKind
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import (
    NodeKind,
    container_urn,
    host_urn,
    process_urn,
    unit_urn,
)
from bystack.core.ports.command import CommandKind, CommandRequest, CommandStatus
from bystack.core.ports.watch import (
    MAX_ENTRIES,
    MatchKind,
    WatchEntry,
    WatchKind,
    WatchRejected,
    normalize,
)
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.infra.watch.durable import DurableWatchStore
from bystack.infra.watch.memory import InMemoryWatchStore
from bystack.providers.agent.provider import AgentProvider
from bystack.providers.host.capability import supported_commands
from bystack.providers.host.mapper import map_process, map_unit
from bystack.runtime.writer import PartitionWriter

C1 = "c" * 64
SOURCE = ENGINE


# --------------------------------------------------------------------------
# The watch list: what may be stored
# --------------------------------------------------------------------------


def unit_entry(name: str, **overrides: Any) -> WatchEntry:
    fields: dict[str, Any] = {
        "id": "",
        "engine_id": ENGINE,
        "kind": WatchKind.UNIT,
        "name": name,
    }
    fields.update(overrides)
    return WatchEntry(**fields)


def process_entry(pattern: str, kind: MatchKind = MatchKind.CMDLINE) -> WatchEntry:
    return WatchEntry(
        id="", engine_id=ENGINE, kind=WatchKind.PROCESS, match_kind=kind, pattern=pattern
    )


def test_a_bare_name_becomes_a_service_the_way_systemctl_reads_it() -> None:
    """`systemctl start nginx` means `nginx.service`, and refusing the short
    form would be a pedantry tax paid by whoever is typing at the time."""
    assert normalize(unit_entry("nginx")).name == "nginx.service"
    # A name that already has a suffix keeps it, including the ones nobody
    # types by hand.
    assert normalize(unit_entry("docker.socket")).name == "docker.socket"
    assert normalize(unit_entry("var-lib-docker.mount")).name == "var-lib-docker.mount"


def test_a_name_that_could_not_be_an_identity_is_refused_at_the_edge() -> None:
    """`/` and `:` are URN separators, so a name carrying one would raise on
    the way into the graph -- three layers away from the box it was typed in."""
    with pytest.raises(WatchRejected):
        normalize(unit_entry("etc/nginx"))
    with pytest.raises(WatchRejected):
        normalize(unit_entry("ngin:x"))
    with pytest.raises(WatchRejected):
        normalize(unit_entry("   "))


def test_an_exec_rule_must_be_absolute_because_a_relative_one_matches_nothing() -> None:
    """`/proc/<pid>/exe` always resolves to an absolute path, so a relative
    pattern is a rule that silently never matches -- which is indistinguishable
    from a daemon that is never running."""
    with pytest.raises(WatchRejected):
        normalize(process_entry("mydaemon", MatchKind.EXEC))
    assert normalize(process_entry("/usr/local/bin/mydaemon", MatchKind.EXEC)).pattern


@pytest.mark.asyncio
async def test_the_store_assigns_the_id_and_the_client_never_does() -> None:
    """A client-chosen id is a client that can overwrite somebody else's entry
    by guessing one, and it is free not to allow."""
    store = InMemoryWatchStore()
    stored = await store.add(unit_entry("nginx"))
    assert stored.id
    assert stored.added_at > 0

    second = await store.add(unit_entry("redis"))
    assert second.id != stored.id


@pytest.mark.asyncio
async def test_watching_the_same_thing_twice_is_refused_rather_than_deduplicated() -> None:
    """Two entries for one target would be two nodes with the same content and
    different URNs, drawn twice -- and the operator who added the second is
    entitled to know why it did not appear."""
    store = InMemoryWatchStore()
    await store.add(unit_entry("nginx"))
    with pytest.raises(WatchRejected):
        # The short form normalizes onto the stored one, which is the case a
        # comparison of raw input would miss.
        await store.add(unit_entry("nginx.service"))


@pytest.mark.asyncio
async def test_the_ceiling_is_a_bound_on_work_done_on_somebody_elses_machine() -> None:
    store = InMemoryWatchStore()
    for index in range(MAX_ENTRIES):
        await store.add(unit_entry(f"service-{index}"))
    with pytest.raises(WatchRejected):
        await store.add(unit_entry("one-too-many"))

    # And it is per host, not global: a second machine starts from zero.
    await store.add(unit_entry("nginx", engine_id="OTHERENGINE"))


@pytest.mark.asyncio
async def test_a_selection_survives_the_restart_it_cannot_be_rediscovered_after(
    tmp_path: Any,
) -> None:
    """The whole reason this is durable. Nothing in the fleet knows what the
    operator selected, so a Controller that forgot it would come back with a
    map missing things and no way to find out what."""
    store = DurableWatchStore.open(tmp_path)
    stored = await store.add(unit_entry("nginx"))
    await store.add(process_entry("worker.py"))

    reopened = DurableWatchStore.open(tmp_path)
    entries = reopened.entries(ENGINE)
    assert [entry.name for entry in entries if entry.kind is WatchKind.UNIT] == [
        "nginx.service"
    ]
    # The id survives too, which is the part that matters: it is the node's
    # identity, so a restart that reassigned ids would replace every process
    # card and lose whatever was anchored to it.
    assert entries[0].id == stored.id

    assert await reopened.remove(ENGINE, stored.id)
    assert DurableWatchStore.open(tmp_path).entries(ENGINE)[0].kind is not WatchKind.UNIT


@pytest.mark.asyncio
async def test_a_corrupt_watch_file_costs_one_entry_rather_than_the_controller(
    tmp_path: Any,
) -> None:
    """The opposite call from the enrollment registry, deliberately: an
    unreadable allow-list means we do not know who is approved and every answer
    is an outage, while an unreadable watch entry means one card is missing."""
    store = DurableWatchStore.open(tmp_path)
    await store.add(unit_entry("nginx"))

    path = tmp_path / "watchlist.json"
    path.write_text('{"entries": [{"id": "x"}, ' + path.read_text()[12:])

    reopened = DurableWatchStore.open(tmp_path)
    assert [entry.name for entry in reopened.entries(ENGINE)] == ["nginx.service"]


# --------------------------------------------------------------------------
# Mapping: what a watched thing becomes
# --------------------------------------------------------------------------


def unit_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Id": "nginx.service",
        "Description": "A high performance web server",
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "MainPID": 4242,
        "ActiveEnterTimestamp": 1_700_000_000_000_000,
        "NRestarts": 0,
        "Result": "success",
        "ExecMainStatus": 0,
        "FragmentPath": "/usr/lib/systemd/system/nginx.service",
    }
    payload.update(overrides)
    return payload


def test_a_unit_that_is_not_installed_says_so_rather_than_reading_as_stopped() -> None:
    """`inactive` on a card reads as "stopped, start it", and the operator
    clicks a button that cannot work. `not-found` is the actual problem."""
    payload = unit_payload(LoadState="not-found", ActiveState="inactive")
    node, edge = map_unit(SOURCE, ENGINE, payload)
    assert node.status == "not-found"
    assert node.urn == unit_urn(ENGINE, "nginx.service")
    assert edge.kind is EdgeKind.HOSTS
    assert edge.src == host_urn(ENGINE)
    # And nothing can be done to it: offering `start` on a unit systemd will
    # refuse to load is a button whose only outcome is an error toast.
    assert supported_commands(node) == frozenset()


def test_a_running_unit_offers_what_systemd_would_accept() -> None:
    node, _ = map_unit(SOURCE, ENGINE, unit_payload())
    assert node.status == "active"
    assert supported_commands(node) == frozenset(
        {CommandKind.STOP, CommandKind.RESTART, CommandKind.KILL}
    )
    # Not `pause`: systemd has no notion of it, and a button that cannot work
    # is worse than no button.
    assert CommandKind.PAUSE not in supported_commands(node)


def test_the_rows_that_are_noise_on_a_healthy_unit_are_left_out() -> None:
    """`result: success` is true, permanent, and pure noise on every card;
    `exit_code: 0` on a running unit is whatever it was before it started."""
    node, _ = map_unit(SOURCE, ENGINE, unit_payload())
    assert node.attrs["result"] is None
    assert node.attrs["exit_code"] is None
    assert node.attrs["restart_count"] is None
    # systemd's microseconds, in the seconds the rest of the graph speaks.
    assert node.attrs["active_since"] == 1_700_000_000.0

    failed, _ = map_unit(
        SOURCE,
        ENGINE,
        unit_payload(ActiveState="failed", Result="oom-kill", ExecMainStatus=137, NRestarts=4),
    )
    assert failed.attrs["result"] == "oom-kill"
    assert failed.attrs["exit_code"] == 137
    assert failed.attrs["restart_count"] == 4


def test_a_unit_that_never_ran_is_not_claimed_to_have_started_at_the_epoch() -> None:
    """systemd's zero means "never", which is a different thing from 1970."""
    node, _ = map_unit(SOURCE, ENGINE, unit_payload(ActiveEnterTimestamp=0))
    assert node.attrs["active_since"] is None


def process_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "WatchId": "w1",
        "MatchKind": "cmdline",
        "Pattern": "worker.py",
        "Label": "",
        "Total": 1,
        "Instances": [
            {
                "Pid": 4242,
                "Comm": "python3",
                "Cmdline": "python3 worker.py",
                "State": "S",
                "StartedAt": 1_700_000_000,
                "Uid": 1000,
                "Cgroup": "0::/system.slice/worker.service",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_a_process_is_identified_by_the_rule_and_never_by_the_pid() -> None:
    """A pid is recycled by the kernel and changes on exactly the event being
    watched for, so a node keyed by one would be a different node after every
    restart -- and would lose everything anchored to the old one."""
    node, _ = map_process(SOURCE, ENGINE, process_payload())
    assert node.urn == process_urn(ENGINE, "w1")

    respawned = {**process_payload()["Instances"][0], "Pid": 99}
    restarted, _ = map_process(SOURCE, ENGINE, process_payload(Instances=[respawned]))
    assert restarted.urn == node.urn
    # The pid is an attribute, so the restart is still *visible* -- it is
    # simply not an identity.
    assert node.attrs["pids"] == (4242,)
    assert restarted.attrs["pids"] == (99,)


def test_a_rule_matching_nothing_is_an_observation_rather_than_an_absence() -> None:
    node, _ = map_process(SOURCE, ENGINE, process_payload(Instances=[], Total=0))
    assert node.status == "absent"
    # Named from the operator's own pattern, so a card for a daemon that is not
    # running still says which daemon.
    assert node.name == "worker.py"
    assert supported_commands(node) == frozenset()


def test_one_zombie_among_healthy_matches_is_the_news() -> None:
    """Worst-wins. A rule matching a working daemon and one zombie must not
    read as healthy: the zombie is what somebody watches a process for."""
    healthy = process_payload()["Instances"][0]
    node, _ = map_process(
        SOURCE, ENGINE, process_payload(Instances=[healthy, {**healthy, "Pid": 7, "State": "Z"}])
    )
    assert node.status == "zombie"
    # And nothing can be done: signals do nothing to a zombie by definition, so
    # every button would silently fail.
    assert supported_commands(node) == frozenset()


def test_a_running_process_can_be_stopped_but_never_started() -> None:
    """There is no recorded way to launch a bare process, and inventing one
    would mean the Controller holding a command line for the agent to run as
    root -- with no user identity in front of the API (ADR-0014)."""
    node, _ = map_process(SOURCE, ENGINE, process_payload())
    assert node.status == "running"
    assert supported_commands(node) == frozenset(
        {CommandKind.STOP, CommandKind.KILL, CommandKind.PAUSE}
    )
    assert CommandKind.START not in supported_commands(node)
    assert CommandKind.RESTART not in supported_commands(node)


def test_the_correlation_is_drawn_only_to_something_that_is_on_the_map() -> None:
    """An edge to a node that is not in the graph is not a relationship, it is
    a dangling reference the UI would have to render an endpoint for."""
    payload = process_payload(
        Instances=[
            {
                **process_payload()["Instances"][0],
                "Cgroup": f"0::/system.slice/docker-{C1}.scope",
            }
        ]
    )

    node, edges = map_process(SOURCE, ENGINE, payload)
    assert node.attrs["container_id"] == C1
    assert [edge.kind for edge in edges] == [EdgeKind.HOSTS]

    _, with_container = map_process(
        SOURCE, ENGINE, payload, present=frozenset({container_urn(ENGINE, C1)})
    )
    assert EdgeKind.RUNS_IN in [edge.kind for edge in with_container]
    runs_in = next(e for e in with_container if e.kind is EdgeKind.RUNS_IN)
    # Declared by the process, which is what stops a container-scoped
    # reconcile from claiming and deleting it on every frame.
    assert runs_in.src == process_urn(ENGINE, "w1")
    assert runs_in.dst == container_urn(ENGINE, C1)


def test_a_process_inside_a_unit_correlates_to_the_unit() -> None:
    node, edges = map_process(
        SOURCE,
        ENGINE,
        process_payload(),
        present=frozenset({unit_urn(ENGINE, "worker.service")}),
    )
    assert node.attrs["unit"] == "worker.service"
    assert any(e.dst == unit_urn(ENGINE, "worker.service") for e in edges)


# --------------------------------------------------------------------------
# Ingest and dispatch, against a synthetic agent
# --------------------------------------------------------------------------


def make_provider() -> tuple[AgentProvider, InMemoryGraphStore, FakeSession]:
    store = InMemoryGraphStore()
    provider = AgentProvider(SOURCE, PartitionWriter(store, InMemoryEventBus(), SOURCE))
    session = FakeSession(
        capabilities=("commands", "units", "processes", "inventory")
    )
    provider.attach(session)
    return provider, store, session


def unit_entity(name: str, **overrides: Any) -> wire.Entity:
    fields: dict[str, Any] = {
        "name": name,
        "load_state": "loaded",
        "active_state": "active",
        "sub_state": "running",
    }
    fields.update(overrides)
    return wire.Entity(id=name, unit=wire.Unit(**fields))


@pytest.mark.asyncio
async def test_a_watched_unit_reaches_the_graph_and_leaves_it_with_the_watch() -> None:
    provider, store, _ = make_provider()
    await provider.on_frame(hello())
    await provider.on_frame(
        wire.Envelope(
            sync=wire.Sync(slice=wire.SLICE_UNIT, entities=[unit_entity("nginx.service")])
        )
    )
    node = store.node(unit_urn(ENGINE, "nginx.service"))
    assert node is not None and node.status == "active"

    # The operator removed the entry, so the agent's next Sync is empty. The
    # slice is authoritative, so the card goes -- this is the path that would
    # otherwise leave a machine's map showing services nobody is watching.
    await provider.on_frame(wire.Envelope(sync=wire.Sync(slice=wire.SLICE_UNIT, entities=[])))
    assert store.node(unit_urn(ENGINE, "nginx.service")) is None
    # And the host itself is untouched: reconcile is scoped to the slice.
    assert store.node(host_urn(ENGINE)) is not None


@pytest.mark.asyncio
async def test_a_unit_name_that_cannot_be_an_identity_costs_one_card_not_the_host() -> None:
    """A malformed *frame* ends the connection, because we no longer know what
    the agent believes. One unusable id among several is not that: closing the
    stream would take a whole host's topology down for a name somebody
    mistyped, and the id came back through a machine we do not own."""
    provider, store, _ = make_provider()
    await provider.on_frame(hello())
    await provider.on_frame(
        wire.Envelope(
            sync=wire.Sync(
                slice=wire.SLICE_UNIT,
                entities=[
                    wire.Entity(id="etc/passwd", unit=wire.Unit(name="etc/passwd")),
                    unit_entity("nginx.service"),
                ],
            )
        )
    )
    assert store.node(unit_urn(ENGINE, "nginx.service")) is not None
    assert len([n for n in store.snapshot().nodes if n.kind == NodeKind.UNIT]) == 1


@pytest.mark.asyncio
async def test_a_container_appearing_is_what_makes_the_correlation_drawable() -> None:
    """The process slice never sees the container event, so nothing else would
    notice that an edge it could not draw a moment ago is now legal."""
    provider, store, _ = make_provider()
    await provider.on_frame(hello())
    await provider.on_frame(
        wire.Envelope(
            sync=wire.Sync(
                slice=wire.SLICE_PROCESS,
                entities=[
                    wire.Entity(
                        id="w1",
                        process=wire.Process(
                            watch_id="w1",
                            match_kind="cmdline",
                            pattern="worker.py",
                            total=1,
                            instances=[
                                wire.ProcessInstance(
                                    pid=42,
                                    comm="python3",
                                    cmdline="python3 worker.py",
                                    state="S",
                                    cgroup=f"0::/docker/{C1}",
                                )
                            ],
                        ),
                    )
                ],
            )
        )
    )
    edges = store.neighbors(process_urn(ENGINE, "w1"))
    assert not [edge for edge in edges if edge.kind is EdgeKind.RUNS_IN]

    await provider.on_frame(
        wire.Envelope(
            sync=wire.Sync(
                slice=wire.SLICE_CONTAINER,
                entities=[
                    wire.Entity(
                        id=C1,
                        container=wire.Container(id=C1, names=["/api"], state="running"),
                    )
                ],
            )
        )
    )
    edges = store.neighbors(process_urn(ENGINE, "w1"))
    assert [edge.dst for edge in edges if edge.kind is EdgeKind.RUNS_IN] == [
        container_urn(ENGINE, C1)
    ]


@pytest.mark.asyncio
async def test_the_watch_list_reaches_the_agent_as_ids_and_never_as_urns() -> None:
    """The agent does not know the URN scheme and must not learn it -- the line
    in ADR-0009 §1, drawn on the one Controller concept that crosses outbound."""
    provider, _, session = make_provider()
    store = InMemoryWatchStore()
    stored = await store.add(unit_entry("nginx"))

    assert await provider.send_watchlist(store.entries(ENGINE))
    frame = session.sent[-1].watch_list
    assert [entry.name for entry in frame.entries] == ["nginx.service"]
    assert frame.entries[0].id == stored.id
    assert "bystack:" not in str(frame)


@pytest.mark.asyncio
async def test_an_agent_that_cannot_watch_is_told_apart_from_one_that_is_asleep() -> None:
    """Absence of a capability is the same answer for "too old" and "compiled
    out" and, here, for "this machine has no systemd" -- and the operator needs
    to hear it while they are still looking at the box they typed in."""
    store = InMemoryWatchStore()
    await store.add(unit_entry("nginx"))

    provider = AgentProvider(
        SOURCE, PartitionWriter(InMemoryGraphStore(), InMemoryEventBus(), SOURCE)
    )
    provider.attach(FakeSession(capabilities=("commands", "processes")))
    assert not await provider.send_watchlist(store.entries(ENGINE))

    # Disconnected is a different `False`, and neither is an error: the list is
    # durable and the next connection carries it.
    provider.detach("gone")
    assert not await provider.send_watchlist(store.entries(ENGINE))


@pytest.mark.asyncio
async def test_a_command_names_which_namespace_its_target_lives_in() -> None:
    """Container ids, unit names and watch ids overlap in shape, and an agent
    that guessed would eventually send a lifecycle command to the wrong
    subsystem entirely."""
    provider, store, session = make_provider()
    await provider.on_frame(hello())
    await provider.on_frame(
        wire.Envelope(
            sync=wire.Sync(slice=wire.SLICE_UNIT, entities=[unit_entity("nginx.service")])
        )
    )
    node = store.node(unit_urn(ENGINE, "nginx.service"))
    assert node is not None

    async def answer() -> None:
        await asyncio.sleep(0)
        command = session.last_command()
        assert command.target_kind == "unit"
        assert command.target_id == "nginx.service"
        provider._channel.resolve(
            wire.CommandResult(command_id=command.command_id, ok=True)
        )

    task = asyncio.create_task(answer())
    outcome = await provider.execute(
        CommandRequest(kind=CommandKind.RESTART, target=node.urn), node
    )
    await task
    assert outcome.status is CommandStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_the_picker_refuses_with_a_reason_rather_than_an_empty_list() -> None:
    """An empty answer with no reason renders as nothing at all, which reads as
    a feature that failed to load rather than as a machine with no match."""
    provider = AgentProvider(
        SOURCE, PartitionWriter(InMemoryGraphStore(), InMemoryEventBus(), SOURCE)
    )
    result = await provider.inventory("unit", "", 50)
    assert not result.ok and "not currently connected" in (result.reason or "")

    provider.attach(FakeSession(capabilities=("commands",)))
    result = await provider.inventory("unit", "", 50)
    assert not result.ok and "cannot enumerate" in (result.reason or "")


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


def test_the_api_stores_a_selection_for_a_host_that_is_not_connected(controller: Any) -> None:
    """The list is the Controller's, so editing it does not need the machine.
    That is the normal case for the laptops and home servers ADR-0008 targets,
    and an edit that failed while a host was asleep would be unusable."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        created = client.post(
            f"/api/v1/agents/{ENGINE}/watch", json={"kind": "unit", "name": "nginx"}
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["entries"][0]["name"] == "nginx.service"
        assert body["entries"][0]["urn"] == str(unit_urn(ENGINE, "nginx.service"))
        assert not body["delivered"]
        assert "no agent has ever connected" in (body["detail"] or "")

        listed = client.get(f"/api/v1/agents/{ENGINE}/watch").json()
        assert len(listed["entries"]) == 1

        entry_id = body["entries"][0]["id"]
        removed = client.delete(f"/api/v1/agents/{ENGINE}/watch/{entry_id}")
        assert removed.status_code == 200
        assert removed.json()["entries"] == []
        assert client.delete(f"/api/v1/agents/{ENGINE}/watch/{entry_id}").status_code == 404


def test_the_api_says_why_it_refused_in_words_meant_for_whoever_typed_it(
    controller: Any,
) -> None:
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        refused = client.post(
            f"/api/v1/agents/{ENGINE}/watch", json={"kind": "unit", "name": "etc/passwd"}
        )
        assert refused.status_code == 422
        assert "unit name" in refused.json()["detail"]

        # A process rule needs to say how to match, and an exec rule needs an
        # absolute path. Both are refused here rather than stored and then
        # never matched on a host.
        assert (
            client.post(
                f"/api/v1/agents/{ENGINE}/watch", json={"kind": "process", "pattern": "x"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/agents/{ENGINE}/watch",
                json={"kind": "process", "match_kind": "exec", "pattern": "relative"},
            ).status_code
            == 422
        )


def test_the_inventory_is_a_404_for_a_host_no_agent_has_ever_dialled(controller: Any) -> None:
    """Distinct from a disconnected agent, which is answered with a reason:
    this one cannot become true by waiting."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        assert client.get(f"/api/v1/agents/{ENGINE}/inventory").status_code == 404
