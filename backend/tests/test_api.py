"""API surface: REST snapshot, WebSocket stream, health.

Routes are exercised against a hand-built store. Nothing here starts a
collector, so a failure in this file is an API failure and never a Docker one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bystack.api.app import API_PREFIX, create_app
from bystack.config import Settings
from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.identity import NodeKind, container_urn, host_urn
from bystack.core.ports.eventbus import Topic

HOST = host_urn("e1")
C1 = container_urn("e1", "c1")


@pytest.fixture
def app():
    # No hosts configured: the collector starts with zero providers, so the
    # API is under test in isolation.
    return create_app(Settings(hosts=[]))


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        seed(app)
        yield client


def seed(app) -> None:
    app.state.context.store.upsert(
        "docker-a",
        [
            Node(urn=HOST, kind=NodeKind.HOST, name="lab-01", source="docker-a"),
            Node(urn=C1, kind=NodeKind.CONTAINER, name="web", source="docker-a", status="running"),
        ],
        [Edge(EdgeKind.HOSTS, HOST, C1, "docker-a")],
    )


def test_health_reports_graph_size_and_read_only_default(client) -> None:
    body = client.get(f"{API_PREFIX}/healthz").json()

    assert body["status"] == "ok"
    assert body["node_count"] == 2
    assert body["edge_count"] == 1
    # Secure default: mutation is opt-in, not opt-out.
    assert body["read_only"] is True


def test_graph_snapshot_carries_a_sequence_number(client) -> None:
    body = client.get(f"{API_PREFIX}/graph").json()

    assert body["type"] == "snapshot"
    assert body["seq"] > 0
    assert {n["urn"] for n in body["nodes"]} == {HOST, C1}


def test_snapshot_can_be_scoped_to_a_partition(client, app) -> None:
    app.state.context.store.upsert(
        "docker-b",
        [Node(urn=host_urn("e2"), kind=NodeKind.HOST, name="lab-02", source="docker-b")],
    )

    body = client.get(f"{API_PREFIX}/graph", params={"sources": ["docker-a"]}).json()

    assert {n["source"] for n in body["nodes"]} == {"docker-a"}


def test_node_lookup_and_404(client) -> None:
    # URNs travel as query parameters: they contain ':' and '/', which makes
    # them hostile as path segments.
    found = client.get(f"{API_PREFIX}/graph/node", params={"urn": C1})
    missing = client.get(
        f"{API_PREFIX}/graph/node", params={"urn": "bystack:container:e1/missing"}
    )

    assert found.json()["name"] == "web"
    assert missing.status_code == 404


def test_malformed_urn_is_a_client_error_not_a_crash(client) -> None:
    response = client.get(f"{API_PREFIX}/graph/node", params={"urn": "not-a-urn"})

    assert response.status_code == 400


def test_relationship_tracing_from_a_node(client) -> None:
    edges = client.get(f"{API_PREFIX}/graph/node/edges", params={"urn": HOST}).json()

    assert [e["dst"] for e in edges] == [C1]


def test_stream_sends_a_snapshot_then_deltas(client, app) -> None:
    with client.websocket_connect(f"{API_PREFIX}/stream") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["seq"] > 0

        delta = app.state.context.store.upsert(
            "docker-a",
            [Node(urn=C1, kind=NodeKind.CONTAINER, name="web", source="docker-a", status="exited")],
        )
        _publish(client, delta)

        message = ws.receive_json()
        assert message["type"] == "delta"
        assert message["seq"] == delta.seq
        assert message["upserted_nodes"][0]["status"] == "exited"


def test_stream_scoped_client_is_silent_during_churn_elsewhere(client, app) -> None:
    # At the stated scale this is a survival condition: a client watching one
    # host must not be woken for every change on the other 499.
    with client.websocket_connect(f"{API_PREFIX}/stream?sources=docker-a") as ws:
        ws.receive_json()  # snapshot

        _publish(
            client,
            app.state.context.store.upsert(
                "docker-b",
                [Node(urn=host_urn("e2"), kind=NodeKind.HOST, name="other", source="docker-b")],
            ),
        )
        _publish(
            client,
            app.state.context.store.upsert(
                "docker-a",
                [Node(urn=C1, kind=NodeKind.CONTAINER, name="web-renamed", source="docker-a")],
            ),
        )

        # The docker-b change is filtered out entirely; the next message the
        # client sees is its own.
        message = ws.receive_json()
        assert message["upserted_nodes"][0]["name"] == "web-renamed"


def test_stream_snapshot_is_scoped_too(client, app) -> None:
    app.state.context.store.upsert(
        "docker-b",
        [Node(urn=host_urn("e2"), kind=NodeKind.HOST, name="other", source="docker-b")],
    )

    with client.websocket_connect(f"{API_PREFIX}/stream?sources=docker-a") as ws:
        snapshot = ws.receive_json()

    assert {n["source"] for n in snapshot["nodes"]} == {"docker-a"}


def test_openapi_documents_the_surface(client) -> None:
    schema = client.get("/openapi.json").json()

    assert f"{API_PREFIX}/graph" in schema["paths"]
    assert f"{API_PREFIX}/healthz" in schema["paths"]


def _publish(client, delta) -> None:
    """Publish a delta from sync test code onto the app's running loop.

    TestClient runs the app on a separate thread; its portal is the supported
    way to call back into that loop.
    """
    bus = client.app.state.context.bus
    client.portal.call(bus.publish, Topic.GRAPH_DELTA, delta)
