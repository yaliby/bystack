"""The agent endpoint, over a real WebSocket.

`test_agent_ingest.py` drives the provider directly and `test_agent_trust.py`
drives the refusals. This file drives the socket: framing, and the fact that a
graph filled by an agent is indistinguishable to every reader above it from
one filled any other way — same `/graph`, same `/healthz`, same delta stream.

Every connection here carries a real client certificate, issued by the real
CA, because since ADR-0011 there is no other kind. The certificate arrives
through the ASGI TLS extension exactly as it does in production; what differs
is only who put it there (see `conftest.WithClientCertificate`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.conftest import Controller, make_controller

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX
from bystack.core.identity import container_urn, host_urn

AGENT_PATH = f"{API_PREFIX}/agents/connect"
ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE = "AAAABBBBCCCC"
C1 = "c" * 64


def hello_frame(engine_id: str = ENGINE_ID, *, read_only: bool = False) -> bytes:
    return wire.Envelope(
        hello=wire.Hello(
            agent_version="0.1.0",
            engine_id=engine_id,
            engine=wire.EngineInfo(id=engine_id, name="lab-node-01", server_version="29.6.0"),
            read_only=read_only,
            unix_time=int(time.time()),
        )
    ).SerializeToString()


def container_sync(container_id: str = C1, state: str = "running") -> bytes:
    return wire.Envelope(
        sync=wire.Sync(
            slice=wire.SLICE_CONTAINER,
            entities=[
                wire.Entity(
                    id=container_id,
                    container=wire.Container(
                        id=container_id,
                        names=["/shop-web-1"],
                        image="nginx:latest",
                        image_id="sha256:abc",
                        state=state,
                        status_text="Up 3 hours",
                        labels={
                            "com.docker.compose.project": "shop",
                            "com.docker.compose.service": "web",
                        },
                    ),
                )
            ],
        )
    ).SerializeToString()


def decode(raw: bytes) -> wire.Envelope:
    envelope = wire.Envelope()
    envelope.ParseFromString(raw)
    return envelope


@pytest.fixture
def admitted(tmp_path: Any) -> tuple[Controller, str]:
    """A Controller with one approved agent, and that agent's certificate."""
    controller = make_controller(tmp_path, auto_approve=True)
    return controller, controller.enroll(ENGINE_ID)


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_the_endpoint_is_closed_until_an_operator_opens_it(tmp_path: Any) -> None:
    """Off by default. Not because we cannot tell who is calling any more, but
    because it binds a port, usually a public one, and a Controller started to
    look at a graph should not open one unasked."""
    controller = make_controller(tmp_path)
    controller.settings.agents.enabled = False
    certificate = controller.enroll(ENGINE_ID)
    client = controller.agent_client(certificate)
    with client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()


def test_the_first_frame_must_be_hello(admitted: tuple[Controller, str]) -> None:
    """A frame before Hello has no partition to be written into, and accepting
    one is how a host's containers end up in another host's graph."""
    controller, certificate = admitted
    client = controller.agent_client(certificate)
    with client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(container_sync())
            socket.receive_bytes()

    assert controller.context.collector.providers == {}


def test_an_undecodable_frame_ends_the_connection(admitted: tuple[Controller, str]) -> None:
    """Unlike a malformed line in the Docker event stream -- which is worth
    ignoring, because the periodic reconcile repairs it -- a frame we cannot
    decode means we do not know what the agent believes. The cheapest way back
    to a known state is a reconnect, which begins with a full Sync."""
    controller, certificate = admitted
    client = controller.agent_client(certificate)
    with client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()  # HelloAck
            socket.send_bytes(b"\xff\xff not protobuf \xff\xff")
            # Bounded, because the handshake queues frames behind the ack -- a
            # watch list, and a renewal offer where one is due -- and they are
            # already in flight when the garbage arrives. The close is what
            # this is about, and it must arrive within a handful of frames
            # rather than "eventually": an unbounded drain would hang CI on a
            # regression instead of failing it.
            for _ in range(5):
                socket.receive_bytes()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_an_agent_is_adopted_and_acknowledged(tmp_path: Any) -> None:
    controller = make_controller(tmp_path, auto_approve=True, resync_interval=600)
    certificate = controller.enroll(ENGINE_ID)
    client = controller.agent_client(certificate)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello_frame())
        ack = decode(socket.receive_bytes()).hello_ack

        assert ack.accepted
        assert ack.resync_interval == 600
        # Identifies this process. An agent seeing a new epoch knows our
        # in-memory graph was dropped and its partition with it.
        assert ack.controller_epoch


def test_a_graph_filled_by_an_agent_is_served_like_any_other(
    admitted: tuple[Controller, str],
) -> None:
    """The point of keeping the mapper and the API untouched: nothing above
    the provider can tell how a node was discovered."""
    controller, certificate = admitted
    agents = controller.agent_client(certificate)
    with (
        TestClient(controller.ui) as browser,
        agents,
        agents.websocket_connect(AGENT_PATH) as socket,
    ):
        socket.send_bytes(hello_frame())
        socket.receive_bytes()
        socket.send_bytes(container_sync())

        body = _graph_containing(browser, str(container_urn(ENGINE, C1)))

    urns = {node["urn"] for node in body["nodes"]}
    assert str(host_urn(ENGINE)) in urns
    assert str(container_urn(ENGINE, C1)) in urns
    # The logical layer, derived on the Controller from labels the agent
    # merely forwarded.
    assert any(node["kind"] == "service" and node["name"] == "web" for node in body["nodes"])


def test_healthz_reports_an_agent_backed_host(admitted: tuple[Controller, str]) -> None:
    controller, certificate = admitted
    agents = controller.agent_client(certificate)
    with (
        TestClient(controller.ui) as browser,
        agents,
        agents.websocket_connect(AGENT_PATH) as socket,
    ):
        socket.send_bytes(hello_frame())
        socket.receive_bytes()
        socket.send_bytes(container_sync())

        provider = _provider_in_state(browser, ENGINE, "ready")

    assert provider["kind"] == "agent"
    assert provider["node_count"] == 1


def test_a_disconnect_leaves_the_graph_and_marks_the_host_degraded(
    admitted: tuple[Controller, str],
) -> None:
    """Stale is not wrong. The last known topology with a clear marker beats
    blanking the screen when a laptop closes its lid."""
    controller, certificate = admitted
    agents = controller.agent_client(certificate)
    with TestClient(controller.ui) as browser, agents:  # noqa: SIM117 - see below
        # Deliberately nested: the assertion is about what happens *after* the
        # agent's socket closes and before the browser's does. Combining these
        # would leave the agent attached and DEGRADED would never arrive.
        with agents.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()
            socket.send_bytes(container_sync())
            _provider_in_state(browser, ENGINE, "ready")

        _provider_in_state(browser, ENGINE, "degraded")
        graph = browser.get(f"{API_PREFIX}/graph").json()

    assert str(container_urn(ENGINE, C1)) in {node["urn"] for node in graph["nodes"]}


def test_an_agent_change_reaches_a_browser_over_the_delta_stream(
    admitted: tuple[Controller, str],
) -> None:
    """End to end: the agent observes, the Controller maps, the browser sees a
    delta. No part of the browser-facing contract changed in the pivot, and
    none of it changed when the agent had to authenticate to be heard."""
    controller, certificate = admitted
    agents = controller.agent_client(certificate)
    with (
        TestClient(controller.ui) as ui,
        agents,
        agents.websocket_connect(AGENT_PATH) as agent,
    ):
        agent.send_bytes(hello_frame())
        agent.receive_bytes()
        agent.send_bytes(container_sync())
        _graph_containing(ui, str(container_urn(ENGINE, C1)))

        with ui.websocket_connect(f"{API_PREFIX}/stream") as browser:
            snapshot = browser.receive_json()
            assert snapshot["type"] == "snapshot"

            # No settling needed: the browser blocks until the delta
            # arrives, which is the thing being tested.
            agent.send_bytes(container_sync(state="exited"))
            delta = browser.receive_json()

    assert delta["type"] == "delta"
    changed = {node["urn"]: node["status"] for node in delta["upserted_nodes"]}
    assert changed[str(container_urn(ENGINE, C1))] == "exited"


def test_an_agent_that_declares_itself_read_only_disables_its_actions(tmp_path: Any) -> None:
    """Advertised at Hello so the UI can disable the buttons rather than offer
    them and watch the agent bounce every one."""
    controller = make_controller(tmp_path, auto_approve=True)
    controller.settings.read_only = False
    certificate = controller.enroll(ENGINE_ID)
    agents = controller.agent_client(certificate)
    with (
        TestClient(controller.ui) as browser,
        agents,
        agents.websocket_connect(AGENT_PATH) as socket,
    ):
        socket.send_bytes(hello_frame(read_only=True))
        socket.receive_bytes()
        socket.send_bytes(container_sync())
        _graph_containing(browser, str(container_urn(ENGINE, C1)))

        actions = browser.get(
            f"{API_PREFIX}/commands/actions",
            params={"urn": str(container_urn(ENGINE, C1))},
        ).json()

    assert actions["commands"] == []


# --------------------------------------------------------------------------
# What the operator surface can see
# --------------------------------------------------------------------------


def test_the_ui_can_tell_that_no_host_can_join(tmp_path: Any) -> None:
    """Minting a token works with the listener off -- deliberately, because it
    is how an operator gets to the point of turning it on. That makes it the
    one thing a UI cannot infer from a successful mint, so it is reported."""
    controller = make_controller(tmp_path)
    controller.settings.agents.enabled = False
    with TestClient(controller.ui) as browser:
        terms = browser.get(f"{API_PREFIX}/agents/enrollment").json()
        minted = browser.post(f"{API_PREFIX}/agents/tokens", json={}).json()

    assert terms["enabled"] is False
    assert terms["listen"] == controller.settings.agents.listen
    # The token is real. Nothing is listening for it, which is the point.
    assert minted["token"].startswith("bst1.")


def test_the_ui_is_told_when_approval_is_automatic(tmp_path: Any) -> None:
    """An operator told to expect a pending row, on a Controller that approves
    on enrollment, would watch for something that is never going to appear."""
    controller = make_controller(tmp_path, auto_approve=True)
    with TestClient(controller.ui) as browser:
        terms = browser.get(f"{API_PREFIX}/agents/enrollment").json()

    assert terms["enabled"] is True
    assert terms["auto_approve"] is True
    assert terms["listen"] == "0.0.0.0:8443"


def test_a_pending_host_is_listed_before_it_is_approved(tmp_path: Any) -> None:
    """What an operator sees in the seconds after pasting the install command.

    The agent has a certificate, is dialling, is being refused, and is
    retrying -- and it must be visible for exactly that reason (ADR-0011: a
    stolen token is *visible* rather than silently effective). Approval is a
    separate question from connection, so the answer does not claim one.
    """
    controller = make_controller(tmp_path)  # auto_approve off: the default
    controller.enroll(ENGINE_ID)

    with TestClient(controller.ui) as browser:
        listed = browser.get(f"{API_PREFIX}/agents").json()
        assert [(a["status"], a["connected"]) for a in listed] == [("pending", False)]

        approved = browser.post(f"{API_PREFIX}/agents/{ENGINE}/approve").json()

    assert approved["status"] == "approved"
    # Approval is not a connection: the agent is on a reconnect backoff and
    # will be along in a second or two. Saying otherwise here would be the
    # collapse `AgentOut.connected` exists to prevent.
    assert approved["connected"] is False


def test_a_revoked_host_is_still_connected_until_its_stream_drops(
    admitted: tuple[Controller, str],
) -> None:
    """The combination that proves the two fields are not one field.

    Revocation is an allow-list check at connection time, not a kill switch
    for a session -- so between revoking and the agent noticing, the host is
    `revoked` *and* streaming. A UI that collapsed status into connectivity
    would have to draw this host as gone while its containers are still
    arriving on the wire.
    """
    controller, certificate = admitted
    agents = controller.agent_client(certificate)
    with (
        TestClient(controller.ui) as browser,
        agents,
        agents.websocket_connect(AGENT_PATH) as socket,
    ):
        socket.send_bytes(hello_frame())
        socket.receive_bytes()
        socket.send_bytes(container_sync())
        _provider_in_state(browser, ENGINE, "ready")

        browser.post(f"{API_PREFIX}/agents/{ENGINE}/revoke")
        # The list is the only route that answers about the stream; approve
        # and revoke answer about the record they just wrote.
        listed = browser.get(f"{API_PREFIX}/agents").json()

    assert [(a["status"], a["connected"]) for a in listed] == [("revoked", True)]


def test_the_actions_offered_agree_with_the_state_on_the_card(tmp_path: Any) -> None:
    """`GET /commands/actions` is state-aware for agent-backed hosts.

    It was not: `AgentProvider` answered with every command it could carry,
    on the grounds that a cached view is staler than the agent's own check
    (`docs/MIGRATION.md` §6.6, recorded as a decision to revisit). The node it
    consults now is the one the dashboard is drawing, so the failure this
    removes is a card that reads `running` above a `Start` button -- which is
    incoherent with itself before it is stale, and the agent still applies the
    authoritative check either way.
    """
    controller = make_controller(tmp_path, auto_approve=True, read_only=False)
    certificate = controller.enroll(ENGINE_ID)
    agents = controller.agent_client(certificate)
    urn = str(container_urn(ENGINE, C1))

    with (
        TestClient(controller.ui) as browser,
        agents,
        agents.websocket_connect(AGENT_PATH) as socket,
    ):
        socket.send_bytes(hello_frame())
        socket.receive_bytes()

        socket.send_bytes(container_sync(state="running"))
        running = _actions_for(browser, urn, "stop")

        socket.send_bytes(container_sync(state="exited"))
        exited = _actions_for(browser, urn, "start")

    # Nothing to start on something already running, and the reverse.
    assert "start" not in running["commands"]
    assert set(running["commands"]) == {"stop", "restart", "pause", "kill"}
    assert set(exited["commands"]) == {"start", "restart"}


def test_a_state_with_no_operations_says_so_rather_than_going_blank(tmp_path: Any) -> None:
    """An empty command list with no reason renders as nothing at all, which
    reads as a feature that failed to load rather than as an answer."""
    controller = make_controller(tmp_path, auto_approve=True, read_only=False)
    certificate = controller.enroll(ENGINE_ID)
    agents = controller.agent_client(certificate)
    urn = str(container_urn(ENGINE, C1))

    with (
        TestClient(controller.ui) as browser,
        agents,
        agents.websocket_connect(AGENT_PATH) as socket,
    ):
        socket.send_bytes(hello_frame())
        socket.receive_bytes()
        # `dead` can only be removed, and this platform does not remove.
        socket.send_bytes(container_sync(state="dead"))
        _graph_containing(browser, urn)
        answer = browser.get(f"{API_PREFIX}/commands/actions", params={"urn": urn}).json()

    assert answer["commands"] == []
    assert answer["reason"] == "unsupported_state"
    assert "dead" in answer["detail"]


def _actions_for(client: TestClient, urn: str, expected: str) -> dict[str, Any]:
    """Poll until the agent's frame has landed and the answer reflects it."""

    def check() -> dict[str, Any] | None:
        body = client.get(f"{API_PREFIX}/commands/actions", params={"urn": urn}).json()
        return body if expected in body.get("commands", []) else None

    return _wait_until(check, f"{expected} to be offered on {urn}")


def _wait_until(check: Callable[[], Any], what: str, timeout: float = 3.0) -> Any:
    """Poll until the server has caught up with what we sent.

    An agent frame is fire-and-forget: nothing comes back to prove it was
    applied, and the endpoint processes it on its own task. Polling the
    observable result is the honest way to wait for that, and it is steadier
    than a sleep, which is either flaky or slow depending on the machine.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _graph_containing(client: TestClient, urn: str) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        body = client.get(f"{API_PREFIX}/graph").json()
        return body if any(node["urn"] == urn for node in body["nodes"]) else None

    return _wait_until(check, f"{urn} in the graph")


def _provider_in_state(client: TestClient, provider_id: str, state: str) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        body = client.get(f"{API_PREFIX}/healthz").json()
        for provider in body["providers"]:
            if provider["id"] == provider_id and provider["state"] == state:
                return provider
        return None

    return _wait_until(check, f"provider {provider_id} to be {state}")
