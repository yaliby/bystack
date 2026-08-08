"""Docker payloads -> canonical graph.

Pure functions, so the subtlest logic in the provider -- deriving the logical
stack/service layer from compose labels -- is verified without a daemon.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from tests.conftest import ENGINE_ID_SAFE, make_container

from bystack.core.graph.model import EdgeKind, Node
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
from bystack.providers.docker.mapper import (
    _parse_depends_on,
    build_partition,
    map_container,
    map_host,
)

SOURCE = "docker-a"
C1 = container_urn(ENGINE_ID_SAFE, "c" * 64)


def edges_of(edges, kind: EdgeKind):
    return [e for e in edges if e.kind == kind]


def test_host_identity_comes_from_the_engine_id(engine_info: dict[str, Any]) -> None:
    host = map_host(SOURCE, engine_info)

    assert host.urn == host_urn(ENGINE_ID_SAFE)
    assert host.kind == NodeKind.HOST
    # Display name is cosmetic and must not participate in identity.
    assert host.name == "lab-node-01"


def test_container_maps_to_physical_and_logical_nodes(container: dict[str, Any]) -> None:
    nodes, _ = map_container(SOURCE, ENGINE_ID_SAFE, container)
    by_urn = {n.urn: n for n in nodes}

    assert container_urn(ENGINE_ID_SAFE, container["Id"]) in by_urn
    assert stack_urn(ENGINE_ID_SAFE, "shop") in by_urn
    assert service_urn(ENGINE_ID_SAFE, "shop", "web") in by_urn


def test_container_without_compose_labels_has_no_logical_layer() -> None:
    payload = make_container("d" * 64, "standalone")
    nodes, edges = map_container(SOURCE, ENGINE_ID_SAFE, payload)

    assert {n.kind for n in nodes} == {NodeKind.CONTAINER}
    assert edges_of(edges, EdgeKind.REALIZED_BY) == []


def test_service_is_realized_by_its_container(container: dict[str, Any]) -> None:
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, container)
    realized = edges_of(edges, EdgeKind.REALIZED_BY)

    assert len(realized) == 1
    assert realized[0].src == service_urn(ENGINE_ID_SAFE, "shop", "web")
    assert realized[0].dst == container_urn(ENGINE_ID_SAFE, container["Id"])


def test_recreating_a_container_preserves_the_service_node() -> None:
    # The property the two-layer identity scheme exists for. Compose recreate
    # mints a new container id; the service the user is watching must not
    # blink out of the graph and take its history with it.
    before = make_container("a" * 64, "web-1", project="shop", service="web")
    after = make_container("b" * 64, "web-1", project="shop", service="web")

    nodes_before, _ = map_container(SOURCE, ENGINE_ID_SAFE, before)
    nodes_after, _ = map_container(SOURCE, ENGINE_ID_SAFE, after)

    services_before = {n.urn for n in nodes_before if n.kind == NodeKind.SERVICE}
    services_after = {n.urn for n in nodes_after if n.kind == NodeKind.SERVICE}
    containers_before = {n.urn for n in nodes_before if n.kind == NodeKind.CONTAINER}
    containers_after = {n.urn for n in nodes_after if n.kind == NodeKind.CONTAINER}

    assert services_before == services_after
    assert containers_before != containers_after


def test_named_volumes_become_nodes_but_bind_mounts_do_not(container: dict[str, Any]) -> None:
    # A bind mount is a host path, not an entity the engine owns. Inventing a
    # node for it would put something on the canvas that nothing can manage.
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, container)
    mounts = edges_of(edges, EdgeKind.MOUNTS)

    assert [e.dst for e in mounts] == [volume_urn(ENGINE_ID_SAFE, "shop_data")]


def test_network_attachment_carries_the_address(container: dict[str, Any]) -> None:
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, container)
    attached = edges_of(edges, EdgeKind.ATTACHED_TO)

    assert len(attached) == 1
    assert attached[0].dst == network_urn(ENGINE_ID_SAFE, "net1")
    assert attached[0].attrs["ipv4"] == "172.17.0.2"


def test_unpublished_ports_are_dropped(container: dict[str, Any]) -> None:
    nodes, _ = map_container(SOURCE, ENGINE_ID_SAFE, container)
    ports = next(n for n in nodes if n.kind == NodeKind.CONTAINER).attrs["ports"]

    assert [p["private"] for p in ports] == [80]


def test_published_ports_link_the_container_to_its_host(container: dict[str, Any]) -> None:
    # The only relationship that leaves the engine. Without it the host node
    # has no links at all and every stack renders as an island.
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, container)
    exposed = edges_of(edges, EdgeKind.EXPOSED_ON)

    assert len(exposed) == 1
    assert exposed[0].src == container_urn(ENGINE_ID_SAFE, container["Id"])
    assert exposed[0].dst == host_urn(ENGINE_ID_SAFE)
    assert exposed[0].attrs["published"] == (8080,)


def test_a_container_publishing_nothing_has_no_host_link() -> None:
    payload = make_container("f" * 64, "worker", project="shop", service="worker")
    payload["Ports"] = [{"PrivatePort": 80, "Type": "tcp"}]
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, payload)

    assert edges_of(edges, EdgeKind.EXPOSED_ON) == []


def test_dual_stack_publication_is_one_link_with_one_port() -> None:
    # Docker reports one entry per host-IP binding, so :8080 on IPv4 and IPv6
    # arrives twice. The inspector wants both; the edge label wants one.
    payload = make_container("g" * 64, "web", project="shop", service="web")
    payload["Ports"] = [
        {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"},
        {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "::"},
    ]
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, payload)
    exposed = edges_of(edges, EdgeKind.EXPOSED_ON)

    assert len(exposed) == 1
    assert exposed[0].attrs["published"] == (8080,)
    assert len(exposed[0].attrs["ports"]) == 2


def test_depends_on_becomes_service_level_edges() -> None:
    payload = make_container(
        "e" * 64,
        "web",
        project="shop",
        service="web",
        depends_on="db:service_started:true,cache:service_healthy:false",
    )
    _, edges = map_container(SOURCE, ENGINE_ID_SAFE, payload)
    depends = edges_of(edges, EdgeKind.DEPENDS_ON)

    assert {e.dst for e in depends} == {
        service_urn(ENGINE_ID_SAFE, "shop", "db"),
        service_urn(ENGINE_ID_SAFE, "shop", "cache"),
    }


def test_depends_on_parsing_tolerates_junk() -> None:
    assert _parse_depends_on(None) == ()
    assert _parse_depends_on("") == ()
    assert _parse_depends_on("db") == ("db",)
    assert _parse_depends_on("db:started:true,,cache:x:y") == ("db", "cache")


def test_only_referenced_images_become_nodes(
    engine_info, container, network, volume, image
) -> None:
    # A host with hundreds of cached build layers is a disk-cleanup concern,
    # not topology. Rendering them would bury the infrastructure.
    unused = {"Id": "sha256:unused", "RepoTags": ["old:1"], "Size": 1, "Created": 1}
    nodes, _ = build_partition(
        SOURCE, engine_info, [container], [network], [volume], [image, unused]
    )
    images = {n.urn for n in nodes if n.kind == NodeKind.IMAGE}

    assert images == {image_urn("sha256:abc123")}


def test_partition_is_self_consistent(engine_info, container, network, volume, image) -> None:
    # Every edge endpoint must exist as a node, or the client receives a
    # dangling reference it has no way to resolve.
    nodes, edges = build_partition(
        SOURCE, engine_info, [container], [network], [volume], [image]
    )
    urns = {n.urn for n in nodes}

    dangling = [e for e in edges if e.src not in urns or e.dst not in urns]
    assert dangling == []


def test_redundant_logical_nodes_deduplicate_by_content() -> None:
    # Every container of a stack re-emits the stack node. That redundancy is
    # deliberate -- it keeps the mapper stateless -- and is only free because
    # the copies are content-identical.
    a = make_container("a" * 64, "web-1", project="shop", service="web")
    b = make_container("b" * 64, "web-2", project="shop", service="web")

    nodes_a, _ = map_container(SOURCE, ENGINE_ID_SAFE, a, observed_at=1.0)
    nodes_b, _ = map_container(SOURCE, ENGINE_ID_SAFE, b, observed_at=2.0)

    stack_a = next(n for n in nodes_a if n.kind == NodeKind.STACK)
    stack_b = next(n for n in nodes_b if n.kind == NodeKind.STACK)

    assert stack_a.revision == stack_b.revision


def test_revision_ignores_observation_time(container: dict[str, Any]) -> None:
    # If timestamps counted, every periodic reconcile would mark everything
    # changed and "incremental updates" would silently become a full refresh
    # on a timer.
    first, _ = map_container(SOURCE, ENGINE_ID_SAFE, container, observed_at=1.0)
    second, _ = map_container(SOURCE, ENGINE_ID_SAFE, container, observed_at=9999.0)

    assert first[0].revision == second[0].revision


def test_revision_ignores_volatile_status_text() -> None:
    # Docker's `Status` field renders as "Up 3 hours" and ticks every second.
    # If it reached the content hash, every reconcile would re-emit every
    # container on the host and incremental sync would silently become a full
    # refresh on a timer. Caught only against a live daemon -- a fixture
    # returns a frozen payload and can never surface it.
    early = make_container("i" * 64, "web")
    early["Status"] = "Up 3 seconds"
    later = make_container("i" * 64, "web")
    later["Status"] = "Up 4 hours"

    first, _ = map_container(SOURCE, ENGINE_ID_SAFE, early)
    second, _ = map_container(SOURCE, ENGINE_ID_SAFE, later)

    assert first[0].revision == second[0].revision


def test_revision_is_a_function_of_content_not_of_mapping_type() -> None:
    # `EMPTY` is a MappingProxyType, and `json` cannot serialize one -- it
    # reaches the encoder's fallback rather than its `sort_keys` path. Hashing
    # the repr instead would make the hash insertion-ordered and would give two
    # equal nodes different revisions, so `same_content_as` would report a
    # change on every reconcile for entities that never moved.
    labels = {"com.docker.compose.project": "shop", "role": "web"}
    plain = Node(urn=C1, kind=NodeKind.CONTAINER, name="web", source=SOURCE, labels=labels)
    proxied = Node(
        urn=C1, kind=NodeKind.CONTAINER, name="web", source=SOURCE,
        labels=MappingProxyType(dict(reversed(labels.items()))),
    )

    assert plain.same_content_as(proxied)

    # And the default is not a third spelling of "no labels".
    bare = Node(urn=C1, kind=NodeKind.CONTAINER, name="web", source=SOURCE)
    explicit = Node(
        urn=C1, kind=NodeKind.CONTAINER, name="web", source=SOURCE, labels={}, attrs={}
    )

    assert bare.same_content_as(explicit)


def test_exit_code_is_extracted_from_the_status_line() -> None:
    # The one durable fact in that free-text field, and the one that
    # diagnoses a crash loop.
    payload = make_container("j" * 64, "web", state="exited")
    payload["Status"] = "Exited (137) 3 seconds ago"

    nodes, _ = map_container(SOURCE, ENGINE_ID_SAFE, payload)

    assert nodes[0].attrs["exit_code"] == 137


def test_running_containers_have_no_exit_code() -> None:
    payload = make_container("k" * 64, "web")
    payload["Status"] = "Up 2 hours"

    nodes, _ = map_container(SOURCE, ENGINE_ID_SAFE, payload)

    assert nodes[0].attrs["exit_code"] is None


def test_an_unhealthy_container_is_still_running_and_says_it_is_unhealthy() -> None:
    # Both halves matter. The state must stay `running` because that is what
    # decides which operations are offered, and a failing container's are a
    # running container's. The verdict rides beside it so the map can draw
    # the difference the state cannot express.
    payload = make_container("m" * 64, "web")
    payload["Health"] = "unhealthy"

    nodes, _ = map_container(SOURCE, ENGINE_ID_SAFE, payload)

    assert nodes[0].status == "running"
    assert nodes[0].attrs["health"] == "unhealthy"


def test_a_container_with_no_healthcheck_carries_no_verdict() -> None:
    # Absent rather than a word meaning "none": "this image declares no
    # healthcheck" is a fact about the image, not a health state, and the UI
    # must not have to know a third spelling to ignore it.
    nodes, _ = map_container(SOURCE, ENGINE_ID_SAFE, make_container("n" * 64, "web"))

    assert nodes[0].attrs["health"] is None
