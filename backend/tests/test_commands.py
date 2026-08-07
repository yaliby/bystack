"""Operations: authorization, expansion, dispatch, audit, API.

Nothing here touches Docker. The provider is a scripted fake, which is what
lets the interesting cases -- a host that disconnects mid-command, a target
that never answers, a provider that raises -- be tested at all; none of them
is reproducible against a real daemon on demand.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bystack.api.app import API_PREFIX, create_app
from bystack.config import Settings
from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import (
    NodeKind,
    container_urn,
    host_urn,
    network_urn,
    service_urn,
    stack_urn,
)
from bystack.core.ports.command import (
    AuditEntry,
    CommandKind,
    CommandRejected,
    CommandRequest,
    CommandStatus,
    RejectionReason,
    TargetOutcome,
)
from bystack.core.ports.provider import ProviderHealth, ProviderState
from bystack.infra.audit.memory import InMemoryAuditLog
from bystack.runtime.commands import CommandService

ENGINE = "e1"
SOURCE = "docker-a"

HOST = host_urn(ENGINE)
STACK = stack_urn(ENGINE, "shop")
WEB = service_urn(ENGINE, "shop", "web")
DB = service_urn(ENGINE, "shop", "db")
WEB_1 = container_urn(ENGINE, "c" * 64)
WEB_2 = container_urn(ENGINE, "d" * 64)
DB_1 = container_urn(ENGINE, "e" * 64)
NET = network_urn(ENGINE, "net1")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class FakeProvider:
    """A provider that records commands instead of running them."""

    def __init__(
        self,
        provider_id: str = SOURCE,
        *,
        supports: frozenset[CommandKind] | None = None,
        fail: set[str] | None = None,
        noop: set[str] | None = None,
        hang: set[str] | None = None,
        offline: bool = False,
        raises: bool = False,
    ) -> None:
        self._id = provider_id
        self._supports = (
            supports
            if supports is not None
            else frozenset({CommandKind.START, CommandKind.STOP, CommandKind.RESTART})
        )
        self.fail = fail or set()
        self.noop = noop or set()
        self.hang = hang or set()
        self.offline = offline
        self.raises = raises
        self.executed: list[tuple[CommandKind, str]] = []

    @property
    def id(self) -> str:
        return self._id

    @property
    def kind(self) -> str:
        return "fake"

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def health(self) -> ProviderHealth:
        return ProviderHealth(state=ProviderState.READY)

    def supported_commands(self, node: Node) -> frozenset[CommandKind]:
        if self.offline or node.kind != NodeKind.CONTAINER:
            return frozenset()
        # Mirrors the real policy's shape: state decides.
        if node.status == "exited":
            return frozenset({CommandKind.START, CommandKind.RESTART}) & self._supports
        return self._supports

    async def execute(self, request: CommandRequest, target: Node) -> TargetOutcome:
        if self.offline:
            raise CommandRejected(
                RejectionReason.PROVIDER_UNAVAILABLE, f"host {self._id} is not connected"
            )
        self.executed.append((request.kind, str(target.urn)))
        if str(target.urn) in self.hang:
            await asyncio.sleep(60)
        if self.raises:
            raise RuntimeError("provider defect")
        if str(target.urn) in self.fail:
            return TargetOutcome(target.urn, CommandStatus.FAILED, "engine said no")
        if str(target.urn) in self.noop:
            return TargetOutcome(target.urn, CommandStatus.NOOP, "already there")
        return TargetOutcome(target.urn, CommandStatus.SUCCEEDED)


class ReadOnlyProvider(FakeProvider):
    """A provider that never implements the executor protocol.

    Prometheus, Grafana and every other observational source look like this,
    and the platform must treat them as read-only without being told.
    """

    supported_commands = None  # type: ignore[assignment]
    execute = None  # type: ignore[assignment]


def seeded_store() -> InMemoryGraphStore:
    """One stack, two services, three containers -- one of them stopped."""
    store = InMemoryGraphStore()
    nodes = [
        Node(urn=HOST, kind=NodeKind.HOST, name="lab-01", source=SOURCE, status="up"),
        Node(urn=STACK, kind=NodeKind.STACK, name="shop", source=SOURCE),
        Node(urn=WEB, kind=NodeKind.SERVICE, name="web", source=SOURCE),
        Node(urn=DB, kind=NodeKind.SERVICE, name="db", source=SOURCE),
        Node(urn=WEB_1, kind=NodeKind.CONTAINER, name="shop-web-1", source=SOURCE,
             status="running"),
        Node(urn=WEB_2, kind=NodeKind.CONTAINER, name="shop-web-2", source=SOURCE,
             status="running"),
        Node(urn=DB_1, kind=NodeKind.CONTAINER, name="shop-db-1", source=SOURCE,
             status="exited"),
        Node(urn=NET, kind=NodeKind.NETWORK, name="shop_default", source=SOURCE),
    ]
    edges = [
        Edge(EdgeKind.CONTAINS, STACK, WEB, SOURCE),
        Edge(EdgeKind.CONTAINS, STACK, DB, SOURCE),
        Edge(EdgeKind.REALIZED_BY, WEB, WEB_1, SOURCE),
        Edge(EdgeKind.REALIZED_BY, WEB, WEB_2, SOURCE),
        Edge(EdgeKind.REALIZED_BY, DB, DB_1, SOURCE),
        Edge(EdgeKind.HOSTS, HOST, WEB_1, SOURCE),
    ]
    store.upsert(SOURCE, nodes, edges)
    return store


def build_service(
    provider: FakeProvider | None = None,
    *,
    read_only: bool = False,
    store: InMemoryGraphStore | None = None,
    max_targets: int = 64,
) -> CommandService:
    provider = provider if provider is not None else FakeProvider()
    return CommandService(
        store if store is not None else seeded_store(),
        InMemoryAuditLog(),
        lambda source: provider if source == provider.id else None,
        read_only=read_only,
        max_targets=max_targets,
    )


def request(kind: CommandKind = CommandKind.RESTART, target=WEB_1, **kwargs) -> CommandRequest:
    return CommandRequest(kind=kind, target=target, **kwargs)


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


async def test_read_only_refuses_every_command() -> None:
    service = build_service(read_only=True)

    with pytest.raises(CommandRejected) as caught:
        await service.execute(request())

    assert caught.value.reason is RejectionReason.READ_ONLY


async def test_read_only_is_checked_before_the_target_is_resolved() -> None:
    """Order matters, and this is the test that pins it.

    A read-only control plane answering "no such node" for one URN and
    "read-only" for another is an existence oracle for infrastructure it
    refuses to operate. It is also the shape of bug where someone later
    reorders the checks for readability and quietly opens a hole.
    """
    service = build_service(read_only=True)

    with pytest.raises(CommandRejected) as caught:
        await service.execute(request(target=container_urn(ENGINE, "ghost")))

    assert caught.value.reason is RejectionReason.READ_ONLY


async def test_a_refusal_is_audited() -> None:
    service = build_service(read_only=True)

    with pytest.raises(CommandRejected):
        await service.execute(request(reason="site is down"))

    entry = service.audit.recent()[0]
    assert entry.status is CommandStatus.REJECTED
    assert entry.reason == "site is down"
    assert "read_only" in (entry.detail or "")


async def test_unknown_target_is_rejected() -> None:
    service = build_service()

    with pytest.raises(CommandRejected) as caught:
        await service.execute(request(target=container_urn(ENGINE, "ghost")))

    assert caught.value.reason is RejectionReason.UNKNOWN_TARGET


@pytest.mark.parametrize("target", [HOST, NET])
async def test_nodes_that_cannot_be_operated_on_are_rejected(target) -> None:
    service = build_service()

    with pytest.raises(CommandRejected) as caught:
        await service.execute(request(target=target))

    assert caught.value.reason is RejectionReason.UNSUPPORTED_TARGET


async def test_a_provider_without_the_executor_protocol_is_read_only() -> None:
    """No configuration, no flag, no registration -- just a missing method."""
    service = build_service(ReadOnlyProvider())

    with pytest.raises(CommandRejected) as caught:
        await service.execute(request())

    assert caught.value.reason is RejectionReason.UNSUPPORTED_TARGET


async def test_too_many_targets_refuses_the_whole_request() -> None:
    """Refused, never truncated.

    Half a stack restarted is the worst outcome available here, and it is
    what truncation would produce.
    """
    provider = FakeProvider()
    service = build_service(provider, max_targets=2)

    with pytest.raises(CommandRejected) as caught:
        await service.execute(request(target=STACK))

    assert caught.value.reason is RejectionReason.TOO_MANY_TARGETS
    assert provider.executed == []


# --------------------------------------------------------------------------
# Target expansion
# --------------------------------------------------------------------------


async def test_a_container_target_acts_on_exactly_itself() -> None:
    provider = FakeProvider()
    service = build_service(provider)

    result = await service.execute(request(CommandKind.RESTART, WEB_1))

    assert provider.executed == [(CommandKind.RESTART, str(WEB_1))]
    assert result.status is CommandStatus.SUCCEEDED


async def test_a_service_target_fans_out_to_its_containers() -> None:
    """The operation an operator actually wants.

    "Restart web" must not require them to know which of two replica
    containers currently realizes it -- that is the graph's job.
    """
    provider = FakeProvider()
    service = build_service(provider)

    result = await service.execute(request(CommandKind.RESTART, WEB))

    assert {urn for _, urn in provider.executed} == {str(WEB_1), str(WEB_2)}
    assert len(result.outcomes) == 2


async def test_a_stack_target_fans_out_through_its_services() -> None:
    provider = FakeProvider()
    service = build_service(provider)

    await service.execute(request(CommandKind.STOP, STACK))

    assert {urn for _, urn in provider.executed} == {str(WEB_1), str(WEB_2), str(DB_1)}


async def test_expansion_follows_edge_direction() -> None:
    """A container must not resolve back up to the service that realizes it.

    ``neighbors`` returns edges incident in either direction. Treating an
    incoming ``realized_by`` as outgoing would make every container command
    silently become a service-wide one -- a restart of one replica taking
    down all of them.
    """
    provider = FakeProvider()
    service = build_service(provider)

    await service.execute(request(CommandKind.RESTART, WEB_1))

    assert provider.executed == [(CommandKind.RESTART, str(WEB_1))]


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


async def test_a_command_does_not_mutate_the_graph() -> None:
    """The property the whole design rests on.

    A restart returns without the graph having moved. The topology changes
    when discovery observes the transition -- which is why the UI can never
    display a state the infrastructure did not report.
    """
    store = seeded_store()
    provider = FakeProvider()
    service = build_service(provider, store=store)
    before_seq = store.seq
    before = store.node(WEB_1)

    await service.execute(request(CommandKind.STOP, WEB_1))

    assert store.seq == before_seq
    assert store.node(WEB_1) == before
    assert store.node(WEB_1).status == "running"


async def test_one_failing_target_does_not_abort_the_others() -> None:
    provider = FakeProvider(fail={str(WEB_2)})
    service = build_service(provider)

    result = await service.execute(request(CommandKind.RESTART, WEB))

    assert len(provider.executed) == 2
    assert {o.status for o in result.outcomes} == {
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED,
    }


async def test_a_partial_failure_rolls_up_to_failed() -> None:
    """Worst-wins. A stack where one of three failed is not a success --
    reporting it as one is how an operator walks away from a half-restarted
    stack believing it is fine."""
    service = build_service(FakeProvider(fail={str(DB_1)}))

    result = await service.execute(request(CommandKind.STOP, STACK))

    assert result.status is CommandStatus.FAILED
    assert result.ok is False


async def test_all_targets_unchanged_rolls_up_to_noop() -> None:
    """Distinct from success. Docker's 304 is an unambiguous statement that
    nothing happened, and a restart that changed nothing must not report as
    a restart that worked."""
    service = build_service(FakeProvider(noop={str(WEB_1), str(WEB_2)}))

    result = await service.execute(request(CommandKind.START, WEB))

    assert result.status is CommandStatus.NOOP
    assert result.ok is True


async def test_a_target_that_never_answers_times_out_rather_than_failing() -> None:
    """A stop that outlives its grace period is usually still shutting down.
    Calling it failed invites a retry into an operation already in flight."""
    provider = FakeProvider(hang={str(WEB_1)})
    service = build_service(provider)

    # `timeout=0` is `docker stop -t 0` -- an explicit "do not wait". With the
    # dispatch margin shrunk, the whole deadline is milliseconds. This also
    # pins that the zero survives: read with `or`, it would silently become
    # the engine's ten second default and this test would hang.
    async with asyncio.timeout(5):
        with _fast_deadline(0.05):
            result = await service.execute(request(CommandKind.STOP, WEB_1, timeout=0))

    assert result.outcomes[0].status is CommandStatus.TIMED_OUT
    assert result.status is CommandStatus.TIMED_OUT
    assert "may still complete" in (result.outcomes[0].detail or "")


async def test_a_disconnected_provider_rejects_rather_than_hangs() -> None:
    service = build_service(FakeProvider(offline=True))

    result = await service.execute(request(CommandKind.RESTART, WEB_1))

    assert result.outcomes[0].status is CommandStatus.REJECTED
    assert "not connected" in (result.outcomes[0].detail or "")


async def test_a_provider_defect_is_contained_to_its_target() -> None:
    """A provider raising is our bug, not the operator's. It must not take
    the sibling targets of the same request down with it."""
    provider = FakeProvider(raises=True)
    service = build_service(provider)

    result = await service.execute(request(CommandKind.RESTART, WEB))

    assert len(result.outcomes) == 2
    assert all(o.status is CommandStatus.FAILED for o in result.outcomes)
    assert all("internal error" in (o.detail or "") for o in result.outcomes)


# --------------------------------------------------------------------------
# Available actions
# --------------------------------------------------------------------------


def test_actions_for_a_container_come_from_its_state() -> None:
    service = build_service()

    running = service.actions(WEB_1)
    stopped = service.actions(DB_1)

    assert CommandKind.STOP in running.commands
    assert CommandKind.STOP not in stopped.commands
    assert CommandKind.START in stopped.commands


def test_actions_for_a_mixed_stack_are_the_union_not_the_intersection() -> None:
    """A stack with one crashed container must still offer `start` -- that is
    the operator's entire reason for being on the page. An intersection would
    hide the action exactly when it is needed."""
    service = build_service()

    actions = service.actions(STACK)

    assert CommandKind.START in actions.commands
    assert CommandKind.STOP in actions.commands
    assert len(actions.targets) == 3


def test_actions_are_ordered_least_destructive_first() -> None:
    service = build_service()

    commands = service.actions(STACK).commands

    assert commands.index(CommandKind.START) < commands.index(CommandKind.STOP)


def test_actions_explain_read_only_rather_than_returning_nothing() -> None:
    """An unexplained absence of buttons reads as a broken page."""
    service = build_service(read_only=True)

    actions = service.actions(WEB_1)

    assert actions.commands == ()
    assert actions.reason is RejectionReason.READ_ONLY
    assert actions.detail


def test_actions_for_a_vanished_node_are_not_an_error() -> None:
    """A selected node disappearing mid-deploy is normal, not exceptional."""
    service = build_service()

    actions = service.actions(container_urn(ENGINE, "ghost"))

    assert actions.reason is RejectionReason.UNKNOWN_TARGET


def test_a_disconnected_provider_offers_no_actions() -> None:
    """Honest before the click rather than after. During an outage "restart
    it" is precisely what an operator will try."""
    service = build_service(FakeProvider(offline=True))

    assert service.actions(WEB_1).commands == ()


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


async def test_an_operation_is_audited_with_its_expanded_targets() -> None:
    service = build_service()

    result = await service.execute(request(CommandKind.RESTART, WEB, reason="deploy"))

    entry = service.audit.recent()[0]
    assert entry.id == result.id
    assert entry.status is CommandStatus.SUCCEEDED
    assert set(entry.targets) == {WEB_1, WEB_2}
    assert entry.reason == "deploy"
    assert entry.actor == "anonymous"


async def test_a_failure_records_which_target_failed() -> None:
    service = build_service(FakeProvider(fail={str(DB_1)}))

    await service.execute(request(CommandKind.STOP, STACK))

    entry = service.audit.recent()[0]
    assert entry.status is CommandStatus.FAILED
    assert DB_1.segments[-1][:12] in (entry.detail or "")


def test_the_audit_log_is_bounded_and_newest_first() -> None:
    """Retention is not optional. An unbounded operation log is a leak that
    only appears on the busiest installation."""
    log = InMemoryAuditLog(capacity=3)
    for index in range(5):
        log.record(
            AuditEntry(
                id=str(index), at=float(index), actor="a", kind=CommandKind.STOP, target=WEB_1
            )
        )

    assert len(log) == 3
    assert [entry.id for entry in log.recent()] == ["4", "3", "2"]


def test_finalizing_an_evicted_entry_is_a_no_op() -> None:
    log = InMemoryAuditLog(capacity=1)
    log.record(AuditEntry(id="old", at=0, actor="a", kind=CommandKind.STOP, target=WEB_1))
    log.record(AuditEntry(id="new", at=1, actor="a", kind=CommandKind.STOP, target=WEB_1))

    log.finalize(
        "old",
        AuditEntry(
            id="old", at=0, actor="a", kind=CommandKind.STOP, target=WEB_1,
            status=CommandStatus.SUCCEEDED,
        ),
    )

    assert [entry.id for entry in log.recent()] == ["new"]


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


@pytest.fixture
def operable():
    """An app with mutation enabled and one scripted provider registered."""
    app = create_app(Settings(read_only=False))
    provider = FakeProvider()
    with TestClient(app) as client:
        context = app.state.context
        context.collector.register(provider)
        context.store.upsert(SOURCE, *_seed_payload())
        yield client, provider


def _seed_payload():
    store = seeded_store()
    snapshot = store.snapshot()
    return snapshot.nodes, snapshot.edges


def test_post_command_is_forbidden_in_read_only_mode() -> None:
    app = create_app(Settings())  # read_only defaults to True
    with TestClient(app) as client:
        response = client.post(
            f"{API_PREFIX}/commands", json={"kind": "restart", "target": str(WEB_1)}
        )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "read_only"


def test_post_command_runs_and_reports_per_target_outcomes(operable) -> None:
    client, provider = operable

    response = client.post(
        f"{API_PREFIX}/commands", json={"kind": "restart", "target": str(WEB)}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "succeeded"
    assert len(body["outcomes"]) == 2
    assert len(provider.executed) == 2


def test_a_failed_command_is_still_a_200_with_the_detail_in_the_body(operable) -> None:
    """The request was accepted, authorized and carried out. That a container
    refused is a fact about the infrastructure and belongs where the client
    can show all of it, not collapsed into a status code."""
    client, provider = operable
    provider.fail.add(str(WEB_1))

    response = client.post(
        f"{API_PREFIX}/commands", json={"kind": "restart", "target": str(WEB)}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_unknown_target_is_a_404_and_a_bad_urn_is_a_400(operable) -> None:
    client, _ = operable

    missing = client.post(
        f"{API_PREFIX}/commands",
        json={"kind": "stop", "target": str(container_urn(ENGINE, "ghost"))},
    )
    malformed = client.post(
        f"{API_PREFIX}/commands", json={"kind": "stop", "target": "not-a-urn"}
    )

    assert missing.status_code == 404
    assert malformed.status_code == 400


def test_an_unknown_command_kind_is_rejected_by_validation(operable) -> None:
    client, provider = operable

    response = client.post(
        f"{API_PREFIX}/commands", json={"kind": "remove", "target": str(WEB_1)}
    )

    assert response.status_code == 422
    assert provider.executed == []


def test_actions_endpoint_drives_the_ui(operable) -> None:
    client, _ = operable

    body = client.get(f"{API_PREFIX}/commands/actions", params={"urn": str(DB_1)}).json()

    assert "start" in body["commands"]
    assert "stop" not in body["commands"]
    assert body["targets"] == [str(DB_1)]


def test_audit_endpoint_returns_newest_first(operable) -> None:
    client, _ = operable
    client.post(f"{API_PREFIX}/commands", json={"kind": "stop", "target": str(WEB_1)})
    client.post(f"{API_PREFIX}/commands", json={"kind": "start", "target": str(WEB_1)})

    body = client.get(f"{API_PREFIX}/commands/audit").json()

    assert [entry["kind"] for entry in body] == ["start", "stop"]
    assert body[0]["actor"] == "anonymous"


def test_the_client_cannot_choose_who_the_audit_log_blames(operable) -> None:
    """An attacker-chosen name next to a real operation is worse than no
    attribution: it looks like evidence."""
    client, _ = operable

    response = client.post(
        f"{API_PREFIX}/commands",
        json={"kind": "stop", "target": str(WEB_1), "actor": "root"},
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _fast_deadline:
    """Shrink the dispatch margin so the timeout path is testable in ms."""

    def __init__(self, margin: float) -> None:
        self._margin = margin

    def __enter__(self) -> None:
        from bystack.runtime import commands as module

        self._saved = module.DISPATCH_MARGIN
        module.DISPATCH_MARGIN = self._margin  # type: ignore[misc]

    def __exit__(self, *exc: object) -> None:
        from bystack.runtime import commands as module

        module.DISPATCH_MARGIN = self._saved  # type: ignore[misc]
