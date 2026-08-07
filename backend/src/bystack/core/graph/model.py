"""The canonical graph model.

Everything the platform knows -- discovered by any provider, from any source
-- is a node or an edge. There is one model, shared by discovery,
visualization, management and metrics correlation.

Memory discipline: these types are the hot path. At the stated budget
(< 512MB holding thousands of resources) we cannot afford Pydantic here, so
the kernel uses frozen ``slots`` dataclasses and Pydantic appears only at the
API edge. See ``ARCHITECTURE.md`` section 9.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import blake2b
from types import MappingProxyType
from typing import Any, Final

from bystack.core.identity import URN

#: Shared immutable empty mapping. Nodes overwhelmingly have no labels, and a
#: fresh ``{}`` per node is pure waste at ten thousand nodes.
EMPTY: Final[Mapping[str, Any]] = MappingProxyType({})

#: Length of the truncated content hash. 16 hex chars = 64 bits; collisions
#: are not a correctness risk here, only a missed update, and 2^-64 per
#: comparison is far below the rate at which we drop events anyway.
_REVISION_BYTES: Final = 8


class EdgeKind(StrEnum):
    """Relationships between nodes.

    Direction is always ``src -> dst`` read as an active verb: a host HOSTS a
    container, a container ATTACHED_TO a network.
    """

    HOSTS = "hosts"                 # host    -> container | network | volume
    CONTAINS = "contains"           # stack   -> service
    REALIZED_BY = "realized_by"     # service -> container   (logical->physical)
    ATTACHED_TO = "attached_to"     # container -> network
    EXPOSED_ON = "exposed_on"       # container -> host      (published port)
    MOUNTS = "mounts"               # container -> volume
    USES_IMAGE = "uses_image"       # container -> image
    DEPENDS_ON = "depends_on"       # container | service -> same


#: Which endpoint owns an edge for reconciliation purposes.
#:
#: An edge is declared by exactly one of its endpoints -- the *dependent* one.
#: A container declares which networks it is attached to; a network declares
#: nothing about who attached to it. A host declares nothing at all; the thing
#: being hosted owns that link.
#:
#: This matters because a kind-scoped reconcile has to decide which edges the
#: caller just made a claim about. Using "either endpoint is in scope" instead
#: looks reasonable and is wrong: a network-scoped refresh would claim every
#: ``container ATTACHED_TO network`` edge, delete them all because it never
#: declares them, and have the next container refresh add them straight back.
#: The result is an infinite add/remove flap that no amount of correct
#: behaviour elsewhere can stop.
EDGE_OWNER: Final[Mapping[str, str]] = {
    EdgeKind.HOSTS: "dst",
    EdgeKind.CONTAINS: "dst",
    EdgeKind.REALIZED_BY: "dst",
    EdgeKind.ATTACHED_TO: "src",
    # Both directions of the container/host relationship are owned by the
    # container: HOSTS points at it, EXPOSED_ON points away from it, and in
    # both cases the host is the passive endpoint that declares nothing.
    EdgeKind.EXPOSED_ON: "src",
    EdgeKind.MOUNTS: "src",
    EdgeKind.USES_IMAGE: "src",
    EdgeKind.DEPENDS_ON: "src",
}


def _encode(value: Any) -> Any:
    """Last resort for values ``json`` cannot serialize.

    Mappings are converted rather than stringified. ``str`` alone is not safe
    here: only ``dict`` reaches the encoder's ``sort_keys`` path, so any other
    ``Mapping`` -- ``EMPTY`` and anything built from it -- would be hashed as
    its *repr*, and a repr is insertion-ordered. Two mappings with identical
    content would then hash differently, which turns the equality
    ``same_content_as`` is supposed to prove into a coin flip and re-upserts
    the node on every reconcile.
    """
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


def _content_hash(*parts: Any) -> str:
    """Deterministic content hash used for cheap change detection.

    Deliberately excludes ``observed_at``: if the timestamp were included,
    every periodic reconcile would report every node as changed and the whole
    "incremental updates, no full refreshes" property would collapse into a
    full refresh on a timer.
    """
    payload = json.dumps(parts, sort_keys=True, default=_encode, separators=(",", ":"))
    return blake2b(payload.encode(), digest_size=_REVISION_BYTES).hexdigest()


@dataclass(frozen=True, slots=True)
class Node:
    """A vertex in the infrastructure graph.

    ``labels`` and ``attrs`` are treated as immutable by convention; mutating
    them after construction desynchronizes ``revision`` and the node will stop
    reporting changes.
    """

    urn: URN
    kind: str
    name: str
    source: str
    """Partition key -- the provider instance that discovered this node.

    A provider may only ever write its own partition. This is what makes
    "no provider depends on another provider" mechanically enforceable rather
    than merely a convention.
    """

    status: str | None = None
    labels: Mapping[str, str] = EMPTY
    attrs: Mapping[str, Any] = EMPTY
    observed_at: float = 0.0
    revision: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.revision:
            object.__setattr__(
                self,
                "revision",
                _content_hash(self.urn, self.kind, self.name, self.status, self.labels, self.attrs),
            )

    def same_content_as(self, other: Node) -> bool:
        """O(1) change detection. Used by the informer on every resync."""
        return self.revision == other.revision


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed relationship.

    Edges are first-class and independently identified, because incremental
    diffing needs to add and remove a relationship without touching either
    endpoint.
    """

    kind: EdgeKind
    src: URN
    dst: URN
    source: str
    attrs: Mapping[str, Any] = EMPTY
    observed_at: float = 0.0
    key: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.key:
            object.__setattr__(self, "key", f"{self.kind}|{self.src}|{self.dst}")


#: An edge's identity. Distinct from URN: edges are identified structurally by
#: their endpoints, not by an id assigned by any external system.
EdgeKey = str
