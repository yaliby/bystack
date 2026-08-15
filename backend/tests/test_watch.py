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
import json
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
    MAX_FANOUT,
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
from bystack.runtime.commands import MAX_TARGETS
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


# --------------------------------------------------------------------------
# One selection, several hosts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_entry_chosen_on_its_own_is_a_group_of_one() -> None:
    """Every entry has a group, including the ordinary per-host one, so a UI
    never has to treat "no group" as a fourth case."""
    store = InMemoryWatchStore()
    first = await store.add(unit_entry("nginx"))
    second = await store.add(unit_entry("redis"))
    assert first.group_id
    # Two separate acts of selection, two groups. Nothing joins entries that
    # were merely added to the same host.
    assert first.group_id != second.group_id


@pytest.mark.asyncio
async def test_a_group_is_a_label_and_never_a_membership() -> None:
    """The whole design in one test. Removing one member leaves the others
    exactly as they were: the group owns nothing, so there is nothing to
    reconcile and no way for a host to acquire an entry nobody chose for it."""
    store = InMemoryWatchStore()
    here = await store.add(unit_entry("nginx", group_id="shared"))
    there = await store.add(unit_entry("nginx", engine_id="OTHERENGINE", group_id="shared"))
    assert here.group_id == there.group_id == "shared"

    assert await store.remove(ENGINE, here.id)
    survivor = store.entries("OTHERENGINE")
    assert [entry.id for entry in survivor] == [there.id]
    assert survivor[0].group_id == "shared"


@pytest.mark.asyncio
async def test_a_group_survives_the_restart_that_the_selection_survives(
    tmp_path: Any,
) -> None:
    store = DurableWatchStore.open(tmp_path)
    stored = await store.add(unit_entry("nginx", group_id="shared"))
    assert DurableWatchStore.open(tmp_path).entries(ENGINE)[0].group_id == stored.group_id


@pytest.mark.asyncio
async def test_an_entry_written_before_groups_existed_becomes_a_group_of_one(
    tmp_path: Any,
) -> None:
    """A file from the previous version holds entries that were each chosen on
    their own, so the entry's own id is the honest group id -- and a stable one,
    which matters because this runs on every load rather than once."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "old1",
                        "engine_id": ENGINE,
                        "kind": "unit",
                        "name": "nginx.service",
                        "match_kind": None,
                        "pattern": "",
                        "label": "",
                        "added_at": 1.0,
                    }
                ]
            }
        )
    )
    first = DurableWatchStore.open(tmp_path).entries(ENGINE)[0]
    assert first.group_id == "old1"
    assert DurableWatchStore.open(tmp_path).entries(ENGINE)[0].group_id == "old1"


def test_the_api_fans_one_selection_out_and_reports_each_host_on_its_own(
    controller: Any,
) -> None:
    """Four hosts, one act of selection, four independent entries -- and the
    count each row carries is what lets a panel say where it came from."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        answer = client.post(
            "/api/v1/agents/watch",
            json={"kind": "unit", "name": "nginx", "engine_ids": [ENGINE, "OTHER", "THIRD"]},
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["stored"] == 3
        # Answered in the order they were asked for, so a UI can put the
        # results beside the checkboxes that produced them.
        assert [host["engine_id"] for host in body["hosts"]] == [ENGINE, "OTHER", "THIRD"]
        # None of these hosts has an agent, which is not a failure to store.
        assert all(host["stored"] and not host["delivered"] for host in body["hosts"])

        listed = client.get(f"/api/v1/agents/{ENGINE}/watch").json()["entries"]
        assert len(listed) == 1
        assert listed[0]["group_id"] == body["group_id"]
        assert listed[0]["group_hosts"] == 3

        # And the entry is this host's from here on. Stopping it stops nothing
        # anywhere else; the other two lists are untouched.
        assert client.delete(f"/api/v1/agents/{ENGINE}/watch/{listed[0]['id']}").status_code == 200
        others = client.get("/api/v1/agents/OTHER/watch").json()["entries"]
        assert len(others) == 1
        assert others[0]["group_hosts"] == 2


def test_one_hosts_refusal_does_not_unwind_the_other_hosts(controller: Any) -> None:
    """Eight machines took it and the ninth already had it. Undoing the eight
    would be a worse answer to a duplicate than keeping them."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        client.post(f"/api/v1/agents/{ENGINE}/watch", json={"kind": "unit", "name": "nginx"})

        body = client.post(
            "/api/v1/agents/watch",
            json={"kind": "unit", "name": "nginx", "engine_ids": [ENGINE, "OTHER"]},
        ).json()
        assert body["stored"] == 1
        refused, stored = body["hosts"]
        assert not refused["stored"]
        # In words meant for whoever typed it, and about that host alone.
        assert "already watches" in refused["detail"]
        assert stored["stored"]

        # The host that refused keeps the entry it already had, and that entry
        # keeps its own group: it was chosen separately and it stays that way.
        first = client.get(f"/api/v1/agents/{ENGINE}/watch").json()["entries"][0]
        assert first["group_id"] != body["group_id"]
        assert first["group_hosts"] == 1


def test_a_fanout_is_bounded_because_one_click_becomes_work_on_every_host(
    controller: Any,
) -> None:
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        assert (
            client.post(
                "/api/v1/agents/watch",
                json={
                    "kind": "unit",
                    "name": "nginx",
                    "engine_ids": [f"HOST{index}" for index in range(MAX_FANOUT + 1)],
                },
            ).status_code
            == 422
        )
        # And a fan-out to nobody is not a request that means anything.
        assert (
            client.post(
                "/api/v1/agents/watch",
                json={"kind": "unit", "name": "nginx", "engine_ids": []},
            ).status_code
            == 422
        )


# --------------------------------------------------------------------------
# Operating a group: N commands, on the hosts the operator named
# --------------------------------------------------------------------------


def test_the_fleets_watch_lists_are_readable_in_one_answer(controller: Any) -> None:
    """The operations bar cannot ask a host's own list which *other* machines
    hold this selection -- they are in other partitions -- so there is one read
    that spans them."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        client.post(
            "/api/v1/agents/watch",
            json={"kind": "unit", "name": "nginx", "engine_ids": [ENGINE, "OTHER"]},
        )
        every = client.get("/api/v1/agents/watch").json()
        assert {entry["engine_id"] for entry in every} == {ENGINE, "OTHER"}
        assert len({entry["group_id"] for entry in every}) == 1
        # The URN is on every row, which is what lets the bar key the index by
        # the thing the canvas actually selects.
        assert all(entry["urn"] for entry in every)


def test_a_group_command_on_a_read_only_controller_is_one_refusal_not_eight(
    controller: Any,
) -> None:
    """Read-only is a fact about this Controller rather than about any host, so
    it gets the answer a single command gets -- not a body of identical rows
    that a client has to notice are all the same."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        body = client.post(
            "/api/v1/agents/watch",
            json={"kind": "unit", "name": "nginx", "engine_ids": [ENGINE, "OTHER"]},
        ).json()

        refused = client.post(
            "/api/v1/commands/group",
            json={"kind": "restart", "group_id": body["group_id"], "engine_ids": [ENGINE, "OTHER"]},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"]["reason"] == "read_only"


def test_a_host_that_no_longer_holds_the_entry_is_named_rather_than_skipped(
    controller: Any,
) -> None:
    """Removed since the operator's screen was drawn, or never there. A host
    that silently vanishes from the answer is indistinguishable from one that
    was never asked -- and the operator is entitled to know their `all nine`
    reached eight."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        body = client.post(
            "/api/v1/agents/watch",
            json={"kind": "unit", "name": "nginx", "engine_ids": [ENGINE]},
        ).json()

        answer = client.post(
            "/api/v1/commands/group",
            json={
                "kind": "restart",
                "group_id": body["group_id"],
                # The second host is in the request and not in the group.
                "engine_ids": [ENGINE, "NEVERWATCHED"],
            },
        )
        # Not all read-only: one host refused for a different reason entirely,
        # so the per-host answer is the only honest one.
        assert answer.status_code == 200, answer.text
        rows = {host["engine_id"]: host for host in answer.json()["hosts"]}
        assert rows["NEVERWATCHED"]["status"] == "unknown_target"
        assert "no longer watches" in rows["NEVERWATCHED"]["detail"]
        assert rows[ENGINE]["status"] == "read_only"
        # A host that never ran makes the whole thing a failure: the operator
        # asked for two machines and reached fewer.
        assert answer.json()["status"] == "failed"


def test_a_group_command_is_bounded_by_the_command_limit_not_the_watch_one(
    controller: Any,
) -> None:
    """`MAX_FANOUT` governs how much intent may be stored; this governs how
    many machines one click may restart at once."""
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(controller.ui)
    with client:
        assert (
            client.post(
                "/api/v1/commands/group",
                json={
                    "kind": "restart",
                    "group_id": "g1",
                    "engine_ids": [f"HOST{index}" for index in range(MAX_TARGETS + 1)],
                },
            ).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_a_group_command_reaches_every_host_and_one_failing_spares_the_rest(
    tmp_path: Any,
) -> None:
    """The whole feature in one test: two machines, one selection, one press,
    two independent commands — and the machine that fails does not take the
    other with it.

    The route is called directly rather than over `TestClient` because both
    agents have to be answered *while* the request is in flight, which needs
    them on the same event loop as the dispatch. What `TestClient` would add
    is FastAPI's serialization, and the four tests above already cover that.
    """
    from tests.conftest import make_controller

    from bystack.api.routes.commands import run_group_command
    from bystack.api.schemas import GroupCommandIn

    class Answering(FakeSession):
        """A `FakeSession` that says when a command has reached it.

        The dispatch is sequential — the route awaits one host before it asks
        the next — so the answering task cannot know when to look without
        this. Polling for it would work and would also be a busy loop in a
        test suite that runs on every commit.
        """

        def __init__(self) -> None:
            super().__init__(capabilities=("commands", "units"))
            self.arrived = asyncio.Event()

        async def send(self, envelope: object) -> None:
            await super().send(envelope)
            if isinstance(envelope, wire.Envelope) and envelope.HasField("command"):
                self.arrived.set()

    controller = make_controller(tmp_path, read_only=False)
    context = controller.context
    other = "BBBBCCCCDDDD"

    sessions: dict[str, Answering] = {}
    for engine_id in (ENGINE, other):
        await context.watchlist.add(
            WatchEntry(
                id="",
                engine_id=engine_id,
                kind=WatchKind.UNIT,
                group_id="shared",
                name="nginx.service",
            )
        )
        provider = context.collector.agent_provider(engine_id, create=True)
        assert provider is not None
        session = Answering()
        provider.attach(session)
        sessions[engine_id] = session
        await provider.on_frame(hello(engine_id=engine_id))
        await provider.on_frame(
            wire.Envelope(
                sync=wire.Sync(slice=wire.SLICE_UNIT, entities=[unit_entity("nginx.service")])
            )
        )

    async def answer() -> None:
        """The first host restarts the unit; the second refuses it."""
        for engine_id, ok in ((ENGINE, True), (other, False)):
            provider = context.collector.agent_provider(engine_id, create=False)
            assert provider is not None
            await sessions[engine_id].arrived.wait()
            command = sessions[engine_id].last_command()
            assert command.target_kind == "unit"
            assert command.target_id == "nginx.service"
            provider._channel.resolve(
                wire.CommandResult(
                    command_id=command.command_id,
                    ok=ok,
                    detail="" if ok else "Unit nginx.service not loaded",
                )
            )

    task = asyncio.create_task(answer())
    result = await run_group_command(
        GroupCommandIn(kind=CommandKind.RESTART, group_id="shared", engine_ids=[ENGINE, other]),
        context.commands,
        context,
    )
    await task

    rows = {host.engine_id: host for host in result.hosts}
    assert set(rows) == {ENGINE, other}
    # Both were dispatched to. Each carries its own complete per-target answer,
    # because this is N commands rather than one command with N targets.
    assert all(row.ran and row.result is not None for row in rows.values())
    assert rows[ENGINE].status == str(CommandStatus.SUCCEEDED)
    assert rows[other].status == str(CommandStatus.FAILED)
    # Worst-wins across hosts, the same rule that applies across the targets
    # within one: eight machines restarted and one not is not a success.
    assert result.status == str(CommandStatus.FAILED)
