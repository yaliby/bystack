"""Agent frames -> the canonical graph.

The seam described in ADR-0009 §1: *the Agent detects that something changed,
the Controller decides what it means.* This module is the Controller's half,
and it does exactly two things.

**It speaks Docker's vocabulary back.** The wire messages carry Docker's own
field set, so ingest rebuilds the payload shapes `mapper.py` already consumes
and hands them to the existing, unmodified mapper. That is what keeps the 483
lines of identity and topology logic in one language and one implementation.
The translation is boring on purpose: if it ever needed to know what a field
*means*, the seam would be in the wrong place.

**It holds the slice membership.** This is the part that is not obvious.

An authoritative `Delta` carries every id in the slice but payloads only for
entities whose hash moved (ADR-0009 §2) -- that asymmetry is the entire reason
the protocol is cheap. But `GraphStore.reconcile` needs *nodes*, and nodes need
payloads, including for the ninety-nine containers that did not change. So the
Controller keeps the last payload it was given per id, and a delta is applied
by overwriting the changed ones and dropping every id absent from the frame.

The cache is therefore the Controller's copy of what the agent last reported.
It is ephemeral and rebuilt from the first `Sync` of every connection, which
is what ADR-0001 requires of any state here: reconstructible from providers,
never a source of truth. Its cost is one Docker payload per entity per host --
about 180 KB for a hundred containers, and the reason the resource budget in
ARCHITECTURE section 11 sizes the Controller per fleet rather than per node.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any, Final

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.identity import URN, NodeKind, engine_scope
from bystack.core.ports.provider import GraphWriter
from bystack.providers.docker.mapper import (
    build_container_slice,
    map_host,
    map_image,
    map_network,
    map_volume,
)

log = logging.getLogger(__name__)

#: Which graph kinds each wire slice is authoritative for.
#:
#: Mirrors the informer's `_SCOPES` exactly, because it is the same rule: a
#: container frame owns the logical layer too, since stacks and services are
#: *derived* from container labels. The last container of a service
#: disappearing is what makes that service cease to exist, and if the
#: container slice did not claim service nodes, the service would outlive it.
SLICE_KINDS: Final[dict[int, frozenset[str]]] = {
    wire.SLICE_CONTAINER: frozenset({NodeKind.CONTAINER, NodeKind.STACK, NodeKind.SERVICE}),
    wire.SLICE_NETWORK: frozenset({NodeKind.NETWORK}),
    wire.SLICE_VOLUME: frozenset({NodeKind.VOLUME}),
    wire.SLICE_IMAGE: frozenset({NodeKind.IMAGE}),
}

SLICE_NAME: Final[dict[int, str]] = {
    wire.SLICE_CONTAINER: "container",
    wire.SLICE_NETWORK: "network",
    wire.SLICE_VOLUME: "volume",
    wire.SLICE_IMAGE: "image",
}


class IngestError(ValueError):
    """A frame we cannot act on.

    Raised only for frames that are malformed or arrive out of order -- an
    agent's input is untrusted, and "unspecified slice" is a bug or an attack,
    not a state to accommodate.
    """


class AgentIngest:
    """Turns one agent's frames into one graph partition.

    Holds no connection and performs no I/O. It is handed decoded frames and a
    writer already bound to its partition, which is what makes the whole
    ingest path testable against synthetic frames with no socket anywhere --
    and is also the security control from ADR-0003: the writer has no
    parameter through which another host's partition could be named, so a
    fully compromised agent can lie about its own host and nothing else.
    """

    __slots__ = ("_source", "_writer", "_engine_id", "_engine_info", "_slices")

    def __init__(self, source: str, writer: GraphWriter) -> None:
        self._source = source
        self._writer = writer
        self._engine_id: str | None = None
        self._engine_info: dict[str, Any] | None = None
        # id -> last payload, per slice. The membership cache described above.
        self._slices: dict[int, dict[str, dict[str, Any]]] = {
            slice_id: {} for slice_id in SLICE_KINDS
        }

    @property
    def engine_id(self) -> str | None:
        return self._engine_id

    @property
    def entity_count(self) -> int:
        """Entities this agent currently reports, across every slice.

        Reported as the provider's node count. It is the agent's view rather
        than a query against the store, which is the honest number: the store
        holds every partition, and "how much has this host told us about" is
        the question a per-provider health line is answering.
        """
        return sum(len(entries) for entries in self._slices.values())

    # -- lifecycle ---------------------------------------------------------

    async def on_hello(self, hello: wire.Hello) -> None:
        """Record the host and write its node.

        The engine id arrives here and nowhere else. Everything downstream
        builds URNs from it, so a connection that has not said Hello cannot
        contribute to the graph at all -- which is enforced by
        :meth:`_require_engine` rather than assumed.
        """
        if not hello.engine_id:
            raise IngestError("Hello carried no engine id")

        self._engine_id = engine_scope(hello.engine_id)
        self._engine_info = _engine_info(hello.engine, hello.engine_id)

        # Upsert, not reconcile: the host node is not part of any slice and
        # must survive every slice reconcile that follows.
        await self._writer.upsert([map_host(self._source, self._engine_info)])

    def reset(self) -> None:
        """Forget everything this agent told us.

        Called when the connection drops. The membership cache describes a
        live agent's view; keeping it across a disconnect would let a
        reconnecting agent's first Delta be applied against a picture from
        before the gap, silently resurrecting containers that were removed
        while it was away.

        The *graph* is deliberately not cleared -- the partition stays as last
        observed and the provider reports DEGRADED. Stale is not wrong, and
        the last known topology with a clear marker beats blanking the screen.
        """
        for entries in self._slices.values():
            entries.clear()

    # -- observations ------------------------------------------------------

    async def on_sync(self, frame: wire.Sync) -> None:
        """Apply a full slice. Replaces the membership wholesale."""
        slice_id = _require_slice(frame.slice)
        self._slices[slice_id] = {
            entity.id: _decode(entity) for entity in frame.entities if entity.id
        }
        await self._reconcile(slice_id)

    async def on_delta(self, frame: wire.Delta) -> None:
        """Apply an authoritative delta.

        Three steps, and the order matters. Overwrite the payloads that
        changed; drop every id the frame did not list; then reconcile the
        whole slice from what remains. Doing the drop *after* the overwrite
        means a frame that both changes and removes entities cannot leave a
        removed one behind because its payload was newer.
        """
        slice_id = _require_slice(frame.slice)
        entries = self._slices[slice_id]

        for entity in frame.changed:
            if entity.id:
                entries[entity.id] = _decode(entity)

        present = set(frame.ids)
        # A payload for an id absent from the membership set is contradictory:
        # the frame is simultaneously saying "this changed" and "this is not
        # here". Trusting the membership set is right, because it is the
        # authoritative half.
        for missing in [key for key in entries if key not in present]:
            del entries[missing]

        # An id with no payload and no cached payload means the agent believes
        # we already have something we do not -- the one way this protocol can
        # go wrong. Dropping it is safe (the next resync repairs it) and
        # silence is not, so it is logged.
        unknown = present - entries.keys()
        if unknown:
            log.warning(
                "agent %s delta named %d unknown %s id(s); a resync will repair it",
                self._source, len(unknown), SLICE_NAME.get(slice_id, "?"),
            )

        await self._reconcile(slice_id)

    # -- mapping -----------------------------------------------------------

    async def _reconcile(self, slice_id: int) -> None:
        engine_id = self._require_engine()
        payloads = list(self._slices[slice_id].values())
        nodes, edges = self._map(slice_id, engine_id, payloads)
        await self._writer.reconcile(nodes, edges, kinds=SLICE_KINDS[slice_id])

        # A container change can orphan an image: the last container using it
        # went away, and an image nothing references is disk housekeeping
        # rather than topology. Re-projecting the image slice from cache costs
        # one reconcile that is almost always empty -- and an empty delta never
        # reaches the wire, so a quiet cluster still produces zero bytes.
        if slice_id == wire.SLICE_CONTAINER and self._slices[wire.SLICE_IMAGE]:
            await self._reconcile_images(engine_id)

    async def _reconcile_images(self, engine_id: str) -> None:
        nodes, edges = self._map(
            wire.SLICE_IMAGE, engine_id, list(self._slices[wire.SLICE_IMAGE].values())
        )
        await self._writer.reconcile(nodes, edges, kinds=SLICE_KINDS[wire.SLICE_IMAGE])

    def _map(
        self, slice_id: int, engine_id: str, payloads: Sequence[dict[str, Any]]
    ) -> tuple[list[Node], list[Edge]]:
        match slice_id:
            case wire.SLICE_CONTAINER:
                return build_container_slice(self._source, engine_id, payloads)
            case wire.SLICE_NETWORK:
                mapped = [map_network(self._source, engine_id, p) for p in payloads]
                return [n for n, _ in mapped], [e for _, e in mapped]
            case wire.SLICE_VOLUME:
                mapped = [map_volume(self._source, engine_id, p) for p in payloads]
                return [n for n, _ in mapped], [e for _, e in mapped]
            case _:
                return self._map_images(engine_id, payloads), []

    def _map_images(
        self, engine_id: str, payloads: Sequence[dict[str, Any]]
    ) -> list[Node]:
        """Only images something actually runs.

        A host with three hundred cached build layers is a disk-cleanup
        concern, not topology, and drawing them would bury the infrastructure
        the operator came to look at. `build_partition` applies the same
        filter; here the reference set comes from the container cache rather
        than from a List, which is the only difference the agent model makes.
        """
        referenced: set[URN] = set()
        containers = self._slices[wire.SLICE_CONTAINER].values()
        _, edges = build_container_slice(self._source, engine_id, list(containers))
        for edge in edges:
            if edge.kind == EdgeKind.USES_IMAGE:
                referenced.add(edge.dst)

        nodes = [map_image(self._source, payload) for payload in payloads]
        return [node for node in nodes if node.urn in referenced]

    def _require_engine(self) -> str:
        if self._engine_id is None:
            raise IngestError("observation frame arrived before Hello")
        return self._engine_id


def _require_slice(slice_id: int) -> int:
    if slice_id not in SLICE_KINDS:
        raise IngestError(f"unknown slice {slice_id!r}")
    return slice_id


# --------------------------------------------------------------------------
# Wire -> Docker vocabulary
#
# Deliberately dumb. Each function is a field-for-field restatement of a
# message in the shape `mapper.py` reads, and nothing here interprets
# anything. When a field is added to the mapper it is added to the `.proto`
# and then here, which is the three-file change ADR-0009 chose on purpose:
# it converts "the attribute is silently always null in the UI" into a change
# you cannot forget to make.
# --------------------------------------------------------------------------


def _decode(entity: wire.Entity) -> dict[str, Any]:
    match entity.WhichOneof("body"):
        case "container":
            return _container(entity.container)
        case "network":
            return _network(entity.network)
        case "volume":
            return _volume(entity.volume)
        case "image":
            return _image(entity.image)
        case _:
            raise IngestError(f"entity {entity.id!r} carried no body")


def _container(message: wire.Container) -> dict[str, Any]:
    return {
        "Id": message.id,
        "Names": list(message.names),
        "Image": message.image,
        "ImageID": message.image_id,
        "Command": message.command,
        "Created": message.created,
        "State": message.state,
        # Carried, never hashed by the agent. The mapper takes the exit code
        # out of it and discards the prose -- see the `.proto` comment and
        # README's "things that will bite you".
        "Status": message.status_text,
        "Labels": dict(message.labels),
        "Ports": [
            {
                "PrivatePort": port.private_port,
                # Absent rather than zero when unpublished: the mapper filters
                # on truthiness, and 0 is not a port.
                "PublicPort": port.public_port or None,
                "Type": port.protocol,
                "IP": port.host_ip,
            }
            for port in message.ports
        ],
        "Mounts": [
            {
                "Type": mount.type,
                "Name": mount.name,
                "Destination": mount.destination,
                "Mode": mount.mode,
                "RW": mount.rw,
            }
            for mount in message.mounts
        ],
        "NetworkSettings": {
            "Networks": {
                attachment.name: {
                    "NetworkID": attachment.network_id,
                    "IPAddress": attachment.ipv4,
                    "Aliases": list(attachment.aliases),
                }
                for attachment in message.networks
            }
        },
    }


def _network(message: wire.Network) -> dict[str, Any]:
    return {
        "Id": message.id,
        "Name": message.name,
        "Driver": message.driver,
        "Scope": message.scope,
        "Internal": message.internal,
        "Attachable": message.attachable,
        "Ingress": message.ingress,
        "Labels": dict(message.labels),
        "IPAM": {"Config": [{"Subnet": subnet} for subnet in message.subnets]},
    }


def _volume(message: wire.Volume) -> dict[str, Any]:
    return {
        "Name": message.name,
        "Driver": message.driver,
        "Mountpoint": message.mountpoint,
        "Scope": message.scope,
        "CreatedAt": message.created_at,
        "Labels": dict(message.labels),
    }


def _image(message: wire.Image) -> dict[str, Any]:
    return {
        "Id": message.id,
        "RepoTags": list(message.repo_tags),
        "RepoDigests": list(message.repo_digests),
        "Size": message.size,
        "Created": message.created,
        "Labels": dict(message.labels),
    }


def _engine_info(message: wire.EngineInfo, engine_id: str) -> dict[str, Any]:
    """`GET /info`, restated.

    The id falls back to `Hello.engine_id` because that is the field the
    certificate binds and the one the whole partition is keyed by; an agent
    that filled in only one of the two must not produce a host node under a
    different identity from its own frames.
    """
    return {
        "ID": message.id or engine_id,
        "Name": message.name,
        "ServerVersion": message.server_version,
        "OperatingSystem": message.operating_system,
        "KernelVersion": message.kernel_version,
        "Architecture": message.architecture,
        "NCPU": message.ncpu,
        "MemTotal": message.mem_total,
        "ContainersRunning": message.containers_running,
        "Containers": message.containers_total,
    }


def slice_names(slices: Iterable[int]) -> tuple[str, ...]:
    """Human-readable slice names, for logs and health detail."""
    return tuple(SLICE_NAME.get(slice_id, str(slice_id)) for slice_id in slices)
