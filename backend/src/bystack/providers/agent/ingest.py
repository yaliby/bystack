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
from bystack.core.identity import (
    URN,
    NodeKind,
    container_urn,
    engine_scope,
    unit_urn,
)
from bystack.core.ports.provider import GraphWriter
from bystack.providers.docker.mapper import (
    build_container_slice,
    map_host,
    map_image,
    map_network,
    map_volume,
)
from bystack.providers.host.mapper import build_process_slice, build_unit_slice

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
    wire.SLICE_UNIT: frozenset({NodeKind.UNIT}),
    wire.SLICE_PROCESS: frozenset({NodeKind.PROCESS}),
}

SLICE_NAME: Final[dict[int, str]] = {
    wire.SLICE_CONTAINER: "container",
    wire.SLICE_NETWORK: "network",
    wire.SLICE_VOLUME: "volume",
    wire.SLICE_IMAGE: "image",
    wire.SLICE_UNIT: "unit",
    wire.SLICE_PROCESS: "process",
}

#: The two slices whose entity ids are not machine-generated.
#:
#: A container id is hexadecimal and a volume name is constrained by the
#: daemon; a unit name is whatever the operator typed, and it reaches us back
#: through a process running on a machine we do not own. Both become URN scope
#: segments, where `/` and `:` are the separators -- so an id carrying one is
#: either a bug on the agent or an attempt to forge an identity in another
#: kind's namespace, and neither is a thing to map.
_HOST_SLICES: Final[frozenset[int]] = frozenset({wire.SLICE_UNIT, wire.SLICE_PROCESS})


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
            entity.id: _decode(entity)
            for entity in frame.entities
            if _usable_id(slice_id, entity.id, self._source)
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
            if _usable_id(slice_id, entity.id, self._source):
                entries[entity.id] = _decode(entity)

        present = {
            entity_id for entity_id in frame.ids
            if _usable_id(slice_id, entity_id, self._source)
        }
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
            await self._reproject(wire.SLICE_IMAGE, engine_id)

        # And the same argument one layer down. A watched process is drawn
        # inside the container or unit its cgroup names, but only while that
        # thing is in the graph (`map_process`, `present`) -- so a container
        # starting is what makes the edge legal, and a container going away is
        # what makes it a dangling reference. Neither event is in the process
        # slice, so nothing else would notice it.
        if slice_id in (wire.SLICE_CONTAINER, wire.SLICE_UNIT) and self._slices[
            wire.SLICE_PROCESS
        ]:
            await self._reproject(wire.SLICE_PROCESS, engine_id)

    async def _reproject(self, slice_id: int, engine_id: str) -> None:
        """Re-map a slice from cache, because something *else* moved.

        Never recurses: it maps and reconciles directly rather than going back
        through :meth:`_reconcile`, so a container change cannot start a chain
        of re-projections that ends up back at containers.
        """
        nodes, edges = self._map(
            slice_id, engine_id, list(self._slices[slice_id].values())
        )
        await self._writer.reconcile(nodes, edges, kinds=SLICE_KINDS[slice_id])

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
            case wire.SLICE_UNIT:
                return build_unit_slice(self._source, engine_id, payloads)
            case wire.SLICE_PROCESS:
                return build_process_slice(
                    self._source, engine_id, payloads, present=self._declared(engine_id)
                )
            case _:
                return self._map_images(engine_id, payloads), []

    def _declared(self, engine_id: str) -> frozenset[URN]:
        """What this partition currently says exists, for correlation.

        Read out of the membership caches rather than out of the store, and
        that is the partition rule from ADR-0003 rather than an optimization:
        asking the store would mean reading a graph that holds every host's
        entities, and the question being answered is only ever about this one.
        """
        urns: set[URN] = set()
        for container_id in self._slices[wire.SLICE_CONTAINER]:
            urns.add(container_urn(engine_id, container_id))
        for unit_name in self._slices[wire.SLICE_UNIT]:
            urns.add(unit_urn(engine_id, unit_name))
        return frozenset(urns)

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


def _usable_id(slice_id: int, entity_id: str, source: str) -> bool:
    """Whether an entity id can be part of an identity at all.

    Dropped rather than fatal, unlike a malformed *frame*. A frame we cannot
    decode means we no longer know what the agent believes and the cheapest way
    back is a reconnect; one unusable id among fifty means one card is missing,
    and closing the stream over it would take a host's whole topology down for
    a name somebody mistyped.

    Only the host slices are checked, because only they carry ids a person
    wrote. An image id is `sha256:...` and legitimately contains the character
    this rejects -- `image_urn` splits on it -- so a blanket check here would
    silently drop every image on every host.
    """
    if not entity_id:
        return False
    if slice_id in _HOST_SLICES and ("/" in entity_id or ":" in entity_id):
        log.warning(
            "agent %s reported an unusable %s id %r; dropping it",
            source, SLICE_NAME.get(slice_id, "?"), entity_id[:64],
        )
        return False
    return True


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
        case "unit":
            return _unit(entity.unit)
        case "process":
            return _process(entity.process)
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
        # Already normalized by the agent to one vocabulary, so this is the
        # verdict itself rather than Docker's nested `Health` object -- the
        # mapper reads a string here and the wire is the only place the two
        # spellings a daemon might use are reconciled.
        "Health": message.health,
        # The depth of a crash loop. Docker only serves this on the inspect
        # endpoint, so the agent pays a round trip for it and only for
        # containers it has already seen listed as `restarting` -- which is
        # why it is zero, not absent, for everything else.
        "RestartCount": message.restart_count,
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


def _unit(message: wire.Unit) -> dict[str, Any]:
    """systemd's own property names, restated.

    `Id` rather than `Name` because that is what systemd calls it on the bus,
    and the rule for this file is that it speaks the source's vocabulary back
    unread -- the same reason the container above it says `Names` and `Status`.
    """
    return {
        "Id": message.name,
        "Description": message.description,
        "LoadState": message.load_state,
        "ActiveState": message.active_state,
        "SubState": message.sub_state,
        "UnitFileState": message.unit_file_state,
        "MainPID": message.main_pid,
        # Microseconds, as systemd reports them. The mapper converts; nothing
        # on the way here is entitled to know which unit this is in.
        "ActiveEnterTimestamp": message.active_enter_timestamp,
        "NRestarts": message.n_restarts,
        "Result": message.result,
        "ExecMainStatus": message.exec_main_status,
        "FragmentPath": message.fragment_path,
    }


def _process(message: wire.Process) -> dict[str, Any]:
    """One watch rule and what currently matches it.

    The only payload in this file that is not a restatement of something the
    source said, because /proc has no document shape to restate: the fields are
    the Controller's own question echoed back beside the kernel's answer.
    """
    return {
        "WatchId": message.watch_id,
        "MatchKind": message.match_kind,
        "Pattern": message.pattern,
        # The operator's own words for this rule, round-tripped through the
        # agent unread. A unit carries none: its name is systemd's, and a card
        # naming it something else would not be the thing you type into
        # `systemctl`.
        "Label": message.label,
        "Total": message.total,
        "Instances": [
            {
                "Pid": instance.pid,
                "Comm": instance.comm,
                "Cmdline": instance.cmdline,
                "State": instance.state,
                "StartedAt": instance.started_at,
                "Uid": instance.uid,
                # Verbatim. Whether this says "inside a container" or "owned by
                # a unit" is interpretation, and interpretation is the
                # Controller's half of the seam (ADR-0009 section 1).
                "Cgroup": instance.cgroup,
            }
            for instance in message.instances
        ],
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
