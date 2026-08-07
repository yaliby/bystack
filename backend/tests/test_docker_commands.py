"""Docker lifecycle operations: state policy and engine status semantics.

The engine is a mock transport, so every status code the Docker API documents
for these endpoints is exercised -- including the ones that are almost
impossible to provoke on demand against a real daemon.
"""

from __future__ import annotations

import httpx
import pytest

from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind, container_urn, network_urn, service_urn
from bystack.core.ports.command import CommandKind, CommandRequest, CommandStatus
from bystack.core.ports.transport import ChannelEndpoint
from bystack.providers.docker.client import ActionResult, EngineClient
from bystack.providers.docker.commands import execute, supported_commands

ENGINE = "e1"
CID = "c" * 64
URN = container_urn(ENGINE, CID)


def container(status: str) -> Node:
    return Node(
        urn=URN, kind=NodeKind.CONTAINER, name="web", source="docker-a", status=status
    )


def engine(handler) -> EngineClient:
    """An EngineClient wired to a scripted daemon rather than a socket."""
    client = EngineClient(ChannelEndpoint(base_url="http://docker", uds_path=None))
    client._client = httpx.AsyncClient(  # noqa: SLF001 - the seam is the point
        transport=httpx.MockTransport(handler), base_url="http://docker"
    )
    return client


# --------------------------------------------------------------------------
# The state policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", {CommandKind.STOP, CommandKind.RESTART, CommandKind.PAUSE,
                     CommandKind.KILL}),
        ("paused", {CommandKind.UNPAUSE, CommandKind.STOP}),
        ("exited", {CommandKind.START, CommandKind.RESTART}),
        ("created", {CommandKind.START}),
        ("restarting", {CommandKind.STOP, CommandKind.KILL}),
    ],
)
def test_each_container_state_permits_what_it_can_actually_do(state, expected) -> None:
    assert set(supported_commands(container(state))) == expected


@pytest.mark.parametrize("state", ["dead", "removing"])
def test_terminal_states_offer_nothing(state) -> None:
    """A dead container can only be removed, and this platform does not remove
    things yet. Offering a button guaranteed to fail is worse than none."""
    assert supported_commands(container(state)) == frozenset()


def test_an_unrecognized_state_degrades_to_read_only() -> None:
    """A newer daemon inventing a state must produce a read-only view of that
    container, never a traceback in the API."""
    assert supported_commands(container("hibernating")) == frozenset()


@pytest.mark.parametrize(
    "node",
    [
        Node(urn=network_urn(ENGINE, "n1"), kind=NodeKind.NETWORK, name="n", source="s"),
        Node(urn=service_urn(ENGINE, "p", "web"), kind=NodeKind.SERVICE, name="web",
             source="s"),
    ],
)
def test_non_containers_have_no_operations_here(node) -> None:
    """Logical nodes are expanded to containers before reaching a provider, so
    one arriving here is an upstream bug, not a case to serve."""
    assert supported_commands(node) == frozenset()


# --------------------------------------------------------------------------
# Engine status codes
# --------------------------------------------------------------------------


async def test_204_is_a_successful_transition() -> None:
    client = engine(lambda request: httpx.Response(204))

    outcome = await execute(
        client, CommandRequest(CommandKind.START, URN), container("exited")
    )

    assert outcome.status is CommandStatus.SUCCEEDED
    assert outcome.ok


async def test_304_is_a_noop_not_a_success() -> None:
    """Docker is unusually precise here and it is worth preserving: starting an
    already-running container is not a change. Folding this into success would
    report a restart storm as fully effective when it changed nothing."""
    client = engine(lambda request: httpx.Response(304))

    outcome = await execute(
        client, CommandRequest(CommandKind.START, URN), container("running")
    )

    assert outcome.status is CommandStatus.NOOP
    assert outcome.ok


async def test_409_carries_the_daemon_s_own_explanation() -> None:
    """"container abc is not paused" is a better message than any we could
    write, because it is the actual reason."""
    client = engine(
        lambda request: httpx.Response(409, json={"message": "container c is not paused"})
    )

    outcome = await execute(
        client, CommandRequest(CommandKind.UNPAUSE, URN), container("running")
    )

    assert outcome.status is CommandStatus.FAILED
    assert outcome.detail == "container c is not paused"


async def test_404_is_a_race_reported_plainly() -> None:
    """The ordinary outcome of clicking restart on something a deploy is
    already replacing."""
    client = engine(
        lambda request: httpx.Response(404, json={"message": "No such container: c"})
    )

    outcome = await execute(
        client, CommandRequest(CommandKind.RESTART, URN), container("running")
    )

    assert outcome.status is CommandStatus.FAILED
    assert "No such container" in (outcome.detail or "")


async def test_a_transport_failure_is_an_outcome_not_an_exception() -> None:
    """One unreachable target must not abort the other five in a stack-wide
    operation."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    outcome = await execute(
        engine(refuse), CommandRequest(CommandKind.STOP, URN), container("running")
    )

    assert outcome.status is CommandStatus.FAILED
    assert "connection refused" in (outcome.detail or "")


async def test_an_unparseable_error_body_is_still_reported() -> None:
    """An unreadable error during an incident is still evidence."""
    client = engine(lambda request: httpx.Response(500, text="<html>gateway</html>"))

    outcome = await execute(
        client, CommandRequest(CommandKind.STOP, URN), container("running")
    )

    assert outcome.status is CommandStatus.FAILED
    assert "500" in (outcome.detail or "")


# --------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------


async def test_the_grace_period_is_sent_only_when_the_operator_set_one() -> None:
    """Omitted rather than defaulted, so a container with its own configured
    grace keeps it instead of being overridden by ours."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = engine(record)
    await execute(client, CommandRequest(CommandKind.STOP, URN), container("running"))
    await execute(
        client, CommandRequest(CommandKind.STOP, URN, timeout=30), container("running")
    )

    assert "t" not in seen[0].url.params
    assert seen[1].url.params["t"] == "30"


async def test_an_explicit_zero_grace_is_sent_not_dropped() -> None:
    """`docker stop -t 0` is a real request -- "do not wait". Read with `or`,
    the zero becomes "unset" and the operator silently gets ten seconds."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await execute(
        engine(record), CommandRequest(CommandKind.STOP, URN, timeout=0), container("running")
    )

    assert seen[0].url.params["t"] == "0"


async def test_the_container_id_comes_from_the_urn_not_a_label() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await execute(
        engine(record), CommandRequest(CommandKind.KILL, URN, signal="SIGTERM"),
        container("running"),
    )

    assert seen[0].url.path == f"/containers/{CID}/kill"
    assert seen[0].url.params["signal"] == "SIGTERM"


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        (CommandKind.START, "start"),
        (CommandKind.STOP, "stop"),
        (CommandKind.RESTART, "restart"),
        (CommandKind.PAUSE, "pause"),
        (CommandKind.UNPAUSE, "unpause"),
        (CommandKind.KILL, "kill"),
    ],
)
async def test_every_command_kind_reaches_its_endpoint(kind, path) -> None:
    """No kind may be silently unroutable: the match in `execute` is
    exhaustive only as long as something checks that it is."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await execute(engine(record), CommandRequest(kind, URN), container("running"))

    assert seen[0].url.path == f"/containers/{CID}/{path}"


def test_the_action_result_vocabulary_is_closed() -> None:
    """Every member is mapped in `execute`. A new one added without a mapping
    would fall through the match and raise at runtime."""
    assert set(ActionResult) == {
        ActionResult.APPLIED,
        ActionResult.UNCHANGED,
        ActionResult.NOT_FOUND,
        ActionResult.CONFLICT,
        ActionResult.ERROR,
    }
