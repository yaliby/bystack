"""Docker Engine payloads -> canonical graph.

The only place in the Docker provider that knows Docker's JSON shapes. Pure
functions, no I/O, no state -- which is what makes the trickiest logic in the
provider (logical service identity, compose relationships) testable without a
Docker daemon anywhere in sight.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from typing import Any, Final

from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.identity import (
    URN,
    NodeKind,
    container_urn,
    engine_scope,
    host_urn,
    image_urn,
    network_urn,
    service_urn,
    stack_urn,
    volume_urn,
)

# Compose writes its model into container labels. These labels are how a set
# of unrelated containers becomes a stack with services -- the logical layer
# that survives container recreation.
LABEL_PROJECT: Final = "com.docker.compose.project"
LABEL_SERVICE: Final = "com.docker.compose.service"
LABEL_CONTAINER_NUMBER: Final = "com.docker.compose.container-number"
LABEL_DEPENDS_ON: Final = "com.docker.compose.depends_on"
LABEL_WORKING_DIR: Final = "com.docker.compose.project.working_dir"
LABEL_CONFIG_FILES: Final = "com.docker.compose.project.config_files"

#: Matches the exit code inside "Exited (137) 3 seconds ago".
_EXIT_CODE: Final = re.compile(r"Exited \((\d+)\)")


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# Host
# --------------------------------------------------------------------------


def map_host(source: str, info: dict[str, Any], *, observed_at: float | None = None) -> Node:
    """Build the host node from ``GET /info``.

    Identity comes from ``.ID``, the engine's own stable identifier. The
    display name is ``.Name``, which is cosmetic and free to change without
    disturbing the graph.
    """
    engine_id = engine_scope(info["ID"])
    return Node(
        urn=host_urn(engine_id),
        kind=NodeKind.HOST,
        name=info.get("Name") or engine_id[:12],
        source=source,
        status="up",
        attrs={
            "engine_version": info.get("ServerVersion"),
            "os": info.get("OperatingSystem"),
            "kernel": info.get("KernelVersion"),
            "arch": info.get("Architecture"),
            "cpus": info.get("NCPU"),
            "memory_bytes": info.get("MemTotal"),
            "containers_running": info.get("ContainersRunning"),
            "containers_total": info.get("Containers"),
        },
        observed_at=observed_at or _now(),
    )


# --------------------------------------------------------------------------
# Containers, and the logical stack/service layer derived from them
# --------------------------------------------------------------------------


def map_container(
    source: str,
    engine_id: str,
    payload: dict[str, Any],
    *,
    observed_at: float | None = None,
) -> tuple[list[Node], list[Edge]]:
    """Map one ``/containers/json`` entry to nodes and edges.

    Emits up to three nodes: the physical container, and -- when the container
    belongs to a compose project -- the logical stack and service it realizes.
    Stack and service nodes are emitted redundantly by every container that
    belongs to them; the store deduplicates by URN and content hash, so the
    redundancy costs nothing and keeps this function free of cross-container
    state.
    """
    at = observed_at or _now()
    container_id = payload["Id"]
    labels: dict[str, str] = payload.get("Labels") or {}

    urn = container_urn(engine_id, container_id)
    host = host_urn(engine_id)
    ports = _map_ports(payload.get("Ports") or [])

    nodes: list[Node] = [
        Node(
            urn=urn,
            kind=NodeKind.CONTAINER,
            name=_container_name(payload, container_id),
            source=source,
            status=payload.get("State"),
            labels=labels,
            attrs={
                "short_id": container_id[:12],
                "image": payload.get("Image"),
                "command": payload.get("Command"),
                "created": payload.get("Created"),
                # Deliberately NOT Docker's `Status` string. It reads
                # "Up 3 hours" and re-renders every second, so including it
                # would change the content hash on every reconcile and
                # re-emit every container on the host forever -- turning
                # incremental sync into a full refresh on a timer. The UI
                # derives uptime from `created` instead, and the one durable
                # fact buried in that string is extracted below.
                "exit_code": _exit_code(payload.get("Status")),
                "ports": ports,
            },
            observed_at=at,
        )
    ]
    edges: list[Edge] = [Edge(EdgeKind.HOSTS, host, urn, source, observed_at=at)]

    # -- published ports ---------------------------------------------------
    # A published port is the one relationship that crosses out of the
    # engine's own world: it is how anything outside -- another compose
    # project, a browser, a colleague -- actually reaches this container.
    # Without it the host is a card with no links and each stack looks like
    # an island, which is exactly the opposite of the truth.
    #
    # One edge per container, not per port: edges are identified by
    # (kind, src, dst), so a container publishing three ports would otherwise
    # collapse into one edge anyway and lose two of them. The ports ride in
    # attrs instead.
    if ports:
        edges.append(
            Edge(
                EdgeKind.EXPOSED_ON,
                urn,
                host,
                source,
                attrs={"ports": ports, "published": _published_ports(ports)},
                observed_at=at,
            )
        )

    # -- image ------------------------------------------------------------
    image_id = payload.get("ImageID")
    if image_id:
        edges.append(
            Edge(EdgeKind.USES_IMAGE, urn, image_urn(image_id), source, observed_at=at)
        )

    # -- networks ---------------------------------------------------------
    networks = (payload.get("NetworkSettings") or {}).get("Networks") or {}
    for network_name, settings in networks.items():
        network_id = (settings or {}).get("NetworkID")
        if not network_id:
            continue
        edges.append(
            Edge(
                EdgeKind.ATTACHED_TO,
                urn,
                network_urn(engine_id, network_id),
                source,
                attrs={
                    "network_name": network_name,
                    "ipv4": (settings or {}).get("IPAddress") or None,
                    "aliases": tuple((settings or {}).get("Aliases") or ()),
                },
                observed_at=at,
            )
        )

    # -- volumes ----------------------------------------------------------
    for mount in payload.get("Mounts") or []:
        # Bind mounts are host paths, not managed volumes. They are real
        # dependencies but they are not entities the engine owns, so they
        # stay as container attributes rather than becoming phantom nodes.
        if mount.get("Type") != "volume" or not mount.get("Name"):
            continue
        edges.append(
            Edge(
                EdgeKind.MOUNTS,
                urn,
                volume_urn(engine_id, mount["Name"]),
                source,
                attrs={
                    "destination": mount.get("Destination"),
                    "mode": mount.get("Mode"),
                    "rw": mount.get("RW"),
                },
                observed_at=at,
            )
        )

    # -- logical layer: stack / service ------------------------------------
    project = labels.get(LABEL_PROJECT)
    service = labels.get(LABEL_SERVICE)
    if project and service:
        stack = stack_urn(engine_id, project)
        svc = service_urn(engine_id, project, service)

        nodes.append(
            Node(
                urn=stack,
                kind=NodeKind.STACK,
                name=project,
                source=source,
                attrs={
                    "working_dir": labels.get(LABEL_WORKING_DIR),
                    "config_files": labels.get(LABEL_CONFIG_FILES),
                },
                observed_at=at,
            )
        )
        nodes.append(
            Node(
                urn=svc,
                kind=NodeKind.SERVICE,
                name=service,
                source=source,
                attrs={"project": project},
                observed_at=at,
            )
        )
        edges.append(Edge(EdgeKind.HOSTS, host, stack, source, observed_at=at))
        edges.append(Edge(EdgeKind.CONTAINS, stack, svc, source, observed_at=at))
        edges.append(
            Edge(
                EdgeKind.REALIZED_BY,
                svc,
                urn,
                source,
                attrs={"replica": labels.get(LABEL_CONTAINER_NUMBER, "1")},
                observed_at=at,
            )
        )

        # depends_on is declared intent, not observed state -- it is the only
        # relationship here that Docker cannot show us at runtime, which is
        # exactly why it is worth extracting from the label.
        for dependency in _parse_depends_on(labels.get(LABEL_DEPENDS_ON)):
            edges.append(
                Edge(
                    EdgeKind.DEPENDS_ON,
                    svc,
                    service_urn(engine_id, project, dependency),
                    source,
                    observed_at=at,
                )
            )

    return nodes, edges


def _container_name(payload: dict[str, Any], container_id: str) -> str:
    names: Sequence[str] = payload.get("Names") or ()
    return names[0].lstrip("/") if names else container_id[:12]


def _exit_code(status_text: str | None) -> int | None:
    """Pull the exit code out of ``"Exited (137) 3 seconds ago"``.

    The only durable fact in Docker's free-text status line, and an important
    one: 137 is SIGKILL, 143 is SIGTERM, and a crash loop is diagnosed by its
    exit code. The surrounding prose is discarded because it is a rendered
    relative timestamp, not data.
    """
    if not status_text:
        return None
    match = _EXIT_CODE.search(status_text)
    return int(match.group(1)) if match else None


def _map_ports(ports: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Normalize published ports, dropping unpublished ones.

    Docker reports one entry per host-IP binding, so a port published on both
    IPv4 and IPv6 appears twice. We keep both: which address a service is
    reachable on is operationally significant, and collapsing them would hide
    an accidentally IPv6-only exposure.
    """
    return tuple(
        {
            "private": port.get("PrivatePort"),
            "public": port.get("PublicPort"),
            "protocol": port.get("Type"),
            "host_ip": port.get("IP"),
        }
        for port in ports
        if port.get("PublicPort")
    )


def _published_ports(ports: Sequence[dict[str, Any]]) -> tuple[int, ...]:
    """The distinct host ports, for labelling the link.

    ``_map_ports`` keeps one entry per host-IP binding on purpose, so a port
    published on both IPv4 and IPv6 appears twice. That distinction matters
    when inspecting a container and is pure noise on an edge label, so it is
    collapsed here rather than in the attribute the inspector reads.
    """
    return tuple(sorted({p["public"] for p in ports if p.get("public")}))


def _parse_depends_on(raw: str | None) -> tuple[str, ...]:
    """Parse compose's ``depends_on`` label.

    Format is ``svc:condition:required[,svc:condition:required]``. Only the
    service name is meaningful to the topology; the condition describes
    startup ordering, which belongs to compose, not to us.
    """
    if not raw:
        return ()
    names = []
    for entry in raw.split(","):
        name = entry.split(":", 1)[0].strip()
        if name:
            names.append(name)
    return tuple(names)


# --------------------------------------------------------------------------
# Networks, volumes, images
# --------------------------------------------------------------------------


def map_network(
    source: str, engine_id: str, payload: dict[str, Any], *, observed_at: float | None = None
) -> tuple[Node, Edge]:
    at = observed_at or _now()
    urn = network_urn(engine_id, payload["Id"])
    ipam_configs = ((payload.get("IPAM") or {}).get("Config")) or []
    node = Node(
        urn=urn,
        kind=NodeKind.NETWORK,
        name=payload.get("Name") or payload["Id"][:12],
        source=source,
        labels=payload.get("Labels") or {},
        attrs={
            "driver": payload.get("Driver"),
            "scope": payload.get("Scope"),
            "internal": payload.get("Internal"),
            "attachable": payload.get("Attachable"),
            "ingress": payload.get("Ingress"),
            "subnets": tuple(c.get("Subnet") for c in ipam_configs if c.get("Subnet")),
        },
        observed_at=at,
    )
    return node, Edge(EdgeKind.HOSTS, host_urn(engine_id), urn, source, observed_at=at)


def map_volume(
    source: str, engine_id: str, payload: dict[str, Any], *, observed_at: float | None = None
) -> tuple[Node, Edge]:
    at = observed_at or _now()
    urn = volume_urn(engine_id, payload["Name"])
    node = Node(
        urn=urn,
        kind=NodeKind.VOLUME,
        name=payload["Name"],
        source=source,
        labels=payload.get("Labels") or {},
        attrs={
            "driver": payload.get("Driver"),
            "mountpoint": payload.get("Mountpoint"),
            "scope": payload.get("Scope"),
            "created_at": payload.get("CreatedAt"),
        },
        observed_at=at,
    )
    return node, Edge(EdgeKind.HOSTS, host_urn(engine_id), urn, source, observed_at=at)


def map_image(source: str, payload: dict[str, Any], *, observed_at: float | None = None) -> Node:
    """Map an image, keyed by content digest.

    Because the digest is host-independent, the same image on twenty hosts
    collapses into one node with twenty edges -- cross-host correlation with
    no correlation logic.
    """
    at = observed_at or _now()
    image_id = payload["Id"]
    tags: Sequence[str] = payload.get("RepoTags") or ()
    return Node(
        urn=image_urn(image_id),
        kind=NodeKind.IMAGE,
        name=tags[0] if tags and tags[0] != "<none>:<none>" else image_id.split(":")[-1][:12],
        source=source,
        labels=payload.get("Labels") or {},
        attrs={
            "tags": tuple(tags),
            "digests": tuple(payload.get("RepoDigests") or ()),
            "size_bytes": payload.get("Size"),
            "created": payload.get("Created"),
        },
        observed_at=at,
    )


# --------------------------------------------------------------------------
# Whole-partition assembly (the List half of List/Watch)
# --------------------------------------------------------------------------


def build_container_slice(
    source: str,
    engine_id: str,
    containers: Sequence[dict[str, Any]],
    *,
    observed_at: float | None = None,
) -> tuple[list[Node], list[Edge]]:
    """The container slice and the logical stack/service layer above it.

    Extracted so that an event-driven refresh and the initial List derive the
    logical layer through the identical code path. Two derivations of "what is
    a service" would drift apart, and the divergence would show up as
    topology that changes depending on how it was discovered.
    """
    at = observed_at or _now()
    nodes: list[Node] = []
    edges: list[Edge] = []
    for payload in containers:
        container_nodes, container_edges = map_container(
            source, engine_id, payload, observed_at=at
        )
        nodes.extend(container_nodes)
        edges.extend(container_edges)
    return nodes, edges


def build_partition(
    source: str,
    info: dict[str, Any],
    containers: Sequence[dict[str, Any]],
    networks: Sequence[dict[str, Any]],
    volumes: Sequence[dict[str, Any]],
    images: Sequence[dict[str, Any]],
) -> tuple[list[Node], list[Edge]]:
    """Assemble a complete, self-consistent partition for one engine."""
    at = _now()
    engine_id = engine_scope(info["ID"])

    nodes, edges = build_container_slice(source, engine_id, containers, observed_at=at)
    nodes.insert(0, map_host(source, info, observed_at=at))

    for payload in networks:
        node, edge = map_network(source, engine_id, payload, observed_at=at)
        nodes.append(node)
        edges.append(edge)

    for payload in volumes:
        node, edge = map_volume(source, engine_id, payload, observed_at=at)
        nodes.append(node)
        edges.append(edge)

    # Only images that something actually uses become nodes. A host with 300
    # cached layers of old builds is a disk-cleanup concern, not topology, and
    # rendering it would bury the infrastructure the user came to look at.
    referenced: set[URN] = {
        edge.dst for edge in edges if edge.kind == EdgeKind.USES_IMAGE
    }
    for payload in images:
        node = map_image(source, payload, observed_at=at)
        if node.urn in referenced:
            nodes.append(node)

    return nodes, edges
