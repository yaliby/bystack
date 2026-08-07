"""Graph store semantics.

Most of these tests defend performance and correctness properties that the
resource budget depends on: no delta when nothing changed, no sequence number
burned on a no-op, no orphaned edges, and no provider able to touch another
provider's partition.
"""

from __future__ import annotations

from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import (
    NodeKind,
    container_urn,
    host_urn,
    image_urn,
    network_urn,
    service_urn,
)


def node(urn, kind, source="docker-a", name="n", **attrs) -> Node:
    return Node(urn=urn, kind=kind, name=name, source=source, attrs=attrs)


HOST = host_urn("e1")
C1 = container_urn("e1", "c1")
C2 = container_urn("e1", "c2")
NET = network_urn("e1", "net1")
SVC = service_urn("e1", "shop", "web")
IMG = image_urn("sha256:" + "a" * 64)


def test_upsert_emits_only_what_changed() -> None:
    store = InMemoryGraphStore()
    store.upsert("docker-a", [node(HOST, NodeKind.HOST), node(C1, NodeKind.CONTAINER)])

    # Same content, re-observed. This is the steady state during every
    # reconcile, and it must produce nothing at all.
    delta = store.upsert("docker-a", [node(HOST, NodeKind.HOST), node(C1, NodeKind.CONTAINER)])

    assert delta.is_empty
    assert delta.change_count == 0


def test_noop_does_not_burn_a_sequence_number() -> None:
    # Clients detect dropped messages by sequence continuity. Phantom
    # increments would make a perfectly healthy quiet system look lossy.
    store = InMemoryGraphStore()
    store.upsert("docker-a", [node(C1, NodeKind.CONTAINER)])
    seq_after_write = store.seq

    store.upsert("docker-a", [node(C1, NodeKind.CONTAINER)])

    assert store.seq == seq_after_write


def test_content_change_is_detected() -> None:
    store = InMemoryGraphStore()
    store.upsert("docker-a", [node(C1, NodeKind.CONTAINER, status="running")])
    delta = store.upsert("docker-a", [node(C1, NodeKind.CONTAINER, status="exited")])

    assert [n.urn for n in delta.upserted_nodes] == [C1]
    assert delta.seq == store.seq


def test_removing_a_node_removes_its_edges() -> None:
    # An edge pointing at a node that no longer exists is not a relationship,
    # it is a leak -- and on the wire it is a dangling reference the client
    # would have to defend against.
    store = InMemoryGraphStore()
    store.upsert(
        "docker-a",
        [node(HOST, NodeKind.HOST), node(C1, NodeKind.CONTAINER)],
        [Edge(EdgeKind.HOSTS, HOST, C1, "docker-a")],
    )

    delta = store.remove("docker-a", nodes=[C1])

    assert delta.removed_nodes == (C1,)
    assert len(delta.removed_edges) == 1
    assert store.neighbors(HOST) == ()


def test_reconcile_removes_entities_the_provider_stopped_reporting() -> None:
    store = InMemoryGraphStore()
    store.upsert("docker-a", [node(C1, NodeKind.CONTAINER), node(C2, NodeKind.CONTAINER)])

    delta = store.reconcile("docker-a", [node(C1, NodeKind.CONTAINER)], [])

    assert delta.removed_nodes == (C2,)
    assert store.node(C1) is not None
    assert store.node(C2) is None


def test_kind_scoped_reconcile_leaves_other_kinds_alone() -> None:
    # The primitive that lets a container event trigger "here are all my
    # containers" without implying anything about networks.
    store = InMemoryGraphStore()
    store.upsert(
        "docker-a",
        [node(HOST, NodeKind.HOST), node(C1, NodeKind.CONTAINER), node(NET, NodeKind.NETWORK)],
        [
            Edge(EdgeKind.HOSTS, HOST, C1, "docker-a"),
            Edge(EdgeKind.HOSTS, HOST, NET, "docker-a"),
        ],
    )

    # Container slice is now empty; the network must survive untouched.
    delta = store.reconcile("docker-a", [], [], kinds=frozenset({NodeKind.CONTAINER}))

    assert delta.removed_nodes == (C1,)
    assert store.node(NET) is not None
    assert len(store.neighbors(NET)) == 1


def test_kind_scoped_reconcile_reclaims_orphaned_logical_nodes() -> None:
    # The bug this primitive exists to prevent: stop a stack, and its service
    # nodes linger on the canvas until the next full resync minutes later.
    store = InMemoryGraphStore()
    store.upsert(
        "docker-a",
        [node(C1, NodeKind.CONTAINER), node(SVC, NodeKind.SERVICE)],
        [Edge(EdgeKind.REALIZED_BY, SVC, C1, "docker-a")],
    )

    scope = frozenset({NodeKind.CONTAINER, NodeKind.SERVICE})
    delta = store.reconcile("docker-a", [], [], kinds=scope)

    assert set(delta.removed_nodes) == {C1, SVC}
    assert store.node(SVC) is None


def test_a_provider_cannot_reconcile_away_another_providers_nodes() -> None:
    # Partitioning is what makes "no provider depends on another provider"
    # a structural property rather than a rule someone has to remember.
    store = InMemoryGraphStore()
    store.upsert("docker-a", [node(C1, NodeKind.CONTAINER, source="docker-a")])
    store.upsert("prometheus", [node(C2, NodeKind.CONTAINER, source="prometheus")])

    store.reconcile("docker-a", [], [])

    assert store.node(C1) is None
    assert store.node(C2) is not None


def test_a_shared_node_survives_until_its_last_claimant_lets_go() -> None:
    # Image identity is the content digest and deliberately is not
    # engine-scoped, so two hosts running the same image arrive at the same
    # URN -- that coincidence *is* the cross-host correlation (ADR-0002). It
    # is also the only URN two partitions legitimately both contain, which
    # makes it the one place a partition can reach outside itself.
    #
    # One `docker image prune` on host A must not delete an image host B is
    # still running, nor cut B's edges to it. Nothing reports an error when it
    # does: B's containers simply stop having an image until its next resync.
    store = InMemoryGraphStore()
    a_web, b_web = container_urn("a", "c1"), container_urn("b", "c1")

    for host, container in (("a", a_web), ("b", b_web)):
        store.reconcile(
            host,
            [node(container, NodeKind.CONTAINER, source=host)],
            [Edge(EdgeKind.USES_IMAGE, container, IMG, host)],
            frozenset({NodeKind.CONTAINER}),
        )
        store.reconcile(
            host, [node(IMG, NodeKind.IMAGE, source=host)], [], frozenset({NodeKind.IMAGE})
        )

    delta = store.reconcile("a", [], [], frozenset({NodeKind.IMAGE}))

    assert store.node(IMG) is not None
    assert IMG not in delta.removed_nodes
    assert [e.dst for e in store.neighbors(b_web)] == [IMG]

    # And it does go when the last host stops reporting it.
    delta = store.reconcile("b", [], [], frozenset({NodeKind.IMAGE}))

    assert store.node(IMG) is None
    assert IMG in delta.removed_nodes


def test_removing_a_shared_node_does_not_orphan_the_other_partition() -> None:
    # Same property through `remove()`, which has its own copy of the cascade.
    store = InMemoryGraphStore()
    b_web = container_urn("b", "c1")
    store.upsert("a", [node(IMG, NodeKind.IMAGE, source="a")])
    store.upsert(
        "b",
        [node(IMG, NodeKind.IMAGE, source="b"), node(b_web, NodeKind.CONTAINER, source="b")],
        [Edge(EdgeKind.USES_IMAGE, b_web, IMG, "b")],
    )

    store.remove("a", [IMG])

    assert store.node(IMG) is not None
    assert [e.dst for e in store.neighbors(b_web)] == [IMG]


def test_edge_attribute_change_is_detected() -> None:
    # A container keeping its network attachment but being assigned a new IP.
    store = InMemoryGraphStore()
    store.upsert("docker-a", [], [Edge(EdgeKind.ATTACHED_TO, C1, NET, "docker-a", {"ipv4": "1"})])
    delta = store.upsert(
        "docker-a", [], [Edge(EdgeKind.ATTACHED_TO, C1, NET, "docker-a", {"ipv4": "2"})]
    )

    assert len(delta.upserted_edges) == 1


def test_snapshot_reflects_current_sequence() -> None:
    store = InMemoryGraphStore()
    store.upsert("docker-a", [node(C1, NodeKind.CONTAINER)])
    snapshot = store.snapshot()

    assert snapshot.seq == store.seq
    assert len(snapshot.nodes) == 1


def test_a_network_scoped_reconcile_does_not_claim_container_attachments() -> None:
    # The flap: "either endpoint in scope" would have the network slice delete
    # the attachment edges it never declares, and the container slice restore
    # them on the next refresh -- add, remove, add, remove, forever. Only
    # visible against a live daemon with more than one network.
    store = InMemoryGraphStore()
    store.upsert(
        "docker-a",
        [node(HOST, NodeKind.HOST), node(C1, NodeKind.CONTAINER), node(NET, NodeKind.NETWORK)],
        [
            Edge(EdgeKind.HOSTS, HOST, NET, "docker-a"),
            Edge(EdgeKind.ATTACHED_TO, C1, NET, "docker-a"),
        ],
    )

    # The network slice declares only the host->network link, as map_network does.
    delta = store.reconcile(
        "docker-a",
        [node(NET, NodeKind.NETWORK)],
        [Edge(EdgeKind.HOSTS, HOST, NET, "docker-a")],
        kinds=frozenset({NodeKind.NETWORK}),
    )

    assert delta.removed_edges == ()
    assert len(store.neighbors(C1)) == 1


def test_a_container_scoped_reconcile_owns_its_outgoing_edges() -> None:
    # The other half: the container slice must be able to reclaim an
    # attachment when the container stops using it.
    store = InMemoryGraphStore()
    store.upsert(
        "docker-a",
        [node(C1, NodeKind.CONTAINER), node(NET, NodeKind.NETWORK)],
        [Edge(EdgeKind.ATTACHED_TO, C1, NET, "docker-a")],
    )

    delta = store.reconcile(
        "docker-a",
        [node(C1, NodeKind.CONTAINER)],
        [],
        kinds=frozenset({NodeKind.CONTAINER}),
    )

    assert len(delta.removed_edges) == 1
    assert store.node(NET) is not None


def test_repeated_alternating_scoped_reconciles_are_stable() -> None:
    # Directly asserts the absence of the flap: alternating slice refreshes
    # against an unchanged engine must produce nothing after the first.
    store = InMemoryGraphStore()
    nodes = [node(HOST, NodeKind.HOST), node(C1, NodeKind.CONTAINER), node(NET, NodeKind.NETWORK)]
    container_edges = [
        Edge(EdgeKind.HOSTS, HOST, C1, "docker-a"),
        Edge(EdgeKind.ATTACHED_TO, C1, NET, "docker-a"),
    ]
    network_edges = [Edge(EdgeKind.HOSTS, HOST, NET, "docker-a")]
    store.upsert("docker-a", nodes, container_edges + network_edges)

    seq = store.seq
    for _ in range(5):
        store.reconcile(
            "docker-a", [node(C1, NodeKind.CONTAINER)], container_edges,
            kinds=frozenset({NodeKind.CONTAINER}),
        )
        store.reconcile(
            "docker-a", [node(NET, NodeKind.NETWORK)], network_edges,
            kinds=frozenset({NodeKind.NETWORK}),
        )

    assert store.seq == seq
