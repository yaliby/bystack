"""The agent endpoint, over a real WebSocket.

`test_agent_ingest.py` drives the provider directly. This file drives the
socket: framing, the refusal paths, and the fact that a graph filled by an
agent is indistinguishable to every reader above it from one filled by the
SSH path — same `/graph`, same `/healthz`, same delta stream.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX, create_app
from bystack.config import AgentsConfig, Settings
from bystack.core.identity import container_urn, host_urn

AGENT_PATH = f"{API_PREFIX}/agents/connect"
ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE = "AAAABBBBCCCC"
C1 = "c" * 64


def app_with(**agents: object):
    return create_app(Settings(hosts=[], agents=AgentsConfig(**agents)))


def hello_frame(engine_id: str = ENGINE_ID, *, read_only: bool = False) -> bytes:
    return wire.Envelope(
        hello=wire.Hello(
            agent_version="0.1.0",
            engine_id=engine_id,
            engine=wire.EngineInfo(id=engine_id, name="lab-node-01", server_version="29.6.0"),
            read_only=read_only,
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


# --------------------------------------------------------------------------
# Refusal paths
# --------------------------------------------------------------------------


def test_the_endpoint_is_closed_until_an_operator_opens_it() -> None:
    """Off by default. Until ADR-0011's mTLS lands this endpoint trusts
    whoever reaches it, so it does not listen unless asked."""
    with TestClient(app_with()) as client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()


def test_an_unknown_agent_is_refused_when_auto_approve_is_off() -> None:
    """With auto-approve off, an engine id we have never enrolled is not a
    host — it is something that reached the port."""
    with TestClient(app_with(enabled=True)) as client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()


def test_the_first_frame_must_be_hello() -> None:
    """A frame before Hello has no partition to be written into, and accepting
    one is how a host's containers end up in another host's graph."""
    app = app_with(enabled=True, auto_approve=True)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(container_sync())
            socket.receive_bytes()

    assert app.state.context.collector.providers == {}


def test_an_undecodable_frame_ends_the_connection() -> None:
    """Unlike a malformed line in the Docker event stream -- which is worth
    ignoring, because the periodic reconcile repairs it -- a frame we cannot
    decode means we do not know what the agent believes. The cheapest way back
    to a known state is a reconnect, which begins with a full Sync."""
    app = app_with(enabled=True, auto_approve=True)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()  # HelloAck
            socket.send_bytes(b"\xff\xff not protobuf \xff\xff")
            socket.receive_bytes()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_an_agent_is_adopted_and_acknowledged() -> None:
    app = app_with(enabled=True, auto_approve=True, resync_interval=600)
    with TestClient(app) as client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello_frame())
        ack = decode(socket.receive_bytes()).hello_ack

        assert ack.accepted
        assert ack.resync_interval == 600
        # Identifies this process. An agent seeing a new epoch knows our
        # in-memory graph was dropped and its partition with it.
        assert ack.controller_epoch


def test_a_graph_filled_by_an_agent_is_served_like_any_other() -> None:
    """The point of keeping the mapper and the API untouched: nothing above
    the provider can tell how a node was discovered."""
    app = app_with(enabled=True, auto_approve=True)
    with TestClient(app) as client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello_frame())
        socket.receive_bytes()
        socket.send_bytes(container_sync())

        body = _graph_containing(client, str(container_urn(ENGINE, C1)))

    urns = {node["urn"] for node in body["nodes"]}
    assert str(host_urn(ENGINE)) in urns
    assert str(container_urn(ENGINE, C1)) in urns
    # The logical layer, derived on the Controller from labels the agent
    # merely forwarded.
    assert any(node["kind"] == "service" and node["name"] == "web" for node in body["nodes"])


def test_healthz_reports_an_agent_backed_host() -> None:
    app = app_with(enabled=True, auto_approve=True)
    with TestClient(app) as client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello_frame())
        socket.receive_bytes()
        socket.send_bytes(container_sync())

        provider = _provider_in_state(client, ENGINE, "ready")

    assert provider["kind"] == "agent"
    assert provider["node_count"] == 1


def test_a_disconnect_leaves_the_graph_and_marks_the_host_degraded() -> None:
    """Stale is not wrong. The last known topology with a clear marker beats
    blanking the screen when a laptop closes its lid."""
    app = app_with(enabled=True, auto_approve=True)
    with TestClient(app) as client:
        with client.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello_frame())
            socket.receive_bytes()
            socket.send_bytes(container_sync())
            _provider_in_state(client, ENGINE, "ready")

        _provider_in_state(client, ENGINE, "degraded")
        graph = client.get(f"{API_PREFIX}/graph").json()

    assert str(container_urn(ENGINE, C1)) in {node["urn"] for node in graph["nodes"]}


def test_an_agent_change_reaches_a_browser_over_the_delta_stream() -> None:
    """End to end: the agent observes, the Controller maps, the browser sees a
    delta. No part of the browser-facing contract changed in the pivot."""
    app = app_with(enabled=True, auto_approve=True)
    with TestClient(app) as client, client.websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        agent.receive_bytes()
        agent.send_bytes(container_sync())
        _graph_containing(client, str(container_urn(ENGINE, C1)))

        with client.websocket_connect(f"{API_PREFIX}/stream") as browser:
            snapshot = browser.receive_json()
            assert snapshot["type"] == "snapshot"

            # No settling needed: the browser blocks until the delta arrives,
            # which is the thing being tested.
            agent.send_bytes(container_sync(state="exited"))
            delta = browser.receive_json()

    assert delta["type"] == "delta"
    changed = {node["urn"]: node["status"] for node in delta["upserted_nodes"]}
    assert changed[str(container_urn(ENGINE, C1))] == "exited"


def test_an_agent_that_declares_itself_read_only_disables_its_actions() -> None:
    """Advertised at Hello so the UI can disable the buttons rather than offer
    them and watch the agent bounce every one."""
    app = create_app(
        Settings(
            hosts=[],
            read_only=False,
            agents=AgentsConfig(enabled=True, auto_approve=True),
        )
    )
    with TestClient(app) as client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello_frame(read_only=True))
        socket.receive_bytes()
        socket.send_bytes(container_sync())
        _graph_containing(client, str(container_urn(ENGINE, C1)))

        actions = client.get(
            f"{API_PREFIX}/commands/actions",
            params={"urn": str(container_urn(ENGINE, C1))},
        ).json()

    assert actions["commands"] == []


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
