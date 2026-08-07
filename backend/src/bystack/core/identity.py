"""Canonical entity identity.

The hardest problem in this platform. Every entity anywhere in the graph --
regardless of which provider discovered it -- is named by a URN from this
module. Correlation across providers is nothing more than two providers
independently arriving at the same URN.

Format::

    bystack:<kind>:<scope>

Two identity layers coexist because they have different lifetimes:

* **Physical** -- ``bystack:container:<engine>/<container_id>``.
  A ``docker compose up`` that recreates a container destroys this identity.
* **Logical** -- ``bystack:service:<engine>/<project>/<service>``.
  Survives recreation. Topology and the event timeline anchor here, so a
  redeploy does not erase an entity's history.

Nothing in this module may import anything outside the standard library.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Self

NAMESPACE: Final = "bystack"

# A scope segment may not contain the separators, or parsing becomes ambiguous.
_ILLEGAL_SEGMENT: Final = re.compile(r"[:/]")


class NodeKind(StrEnum):
    """Kinds of node the core kernel knows about.

    Deliberately small and closed. Providers that need a kind beyond this set
    register it through the type registry rather than editing this enum -- the
    kernel treats unknown kinds as opaque, so a plugin never forces a core
    change.
    """

    CLUSTER = "cluster"
    HOST = "host"
    ENGINE = "engine"
    STACK = "stack"
    SERVICE = "service"
    CONTAINER = "container"
    NETWORK = "network"
    VOLUME = "volume"
    IMAGE = "image"


class URNError(ValueError):
    """Raised when a URN is malformed or built from illegal parts."""


class URN(str):
    """An entity identifier.

    Subclasses :class:`str` so it is free to hash, compare, serialize and use
    as a dict key -- which matters, because the graph store keys every index
    by URN and we hold thousands of them under a 512MB budget.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        parts = value.split(":", 2)
        if len(parts) != 3 or parts[0] != NAMESPACE or not parts[1] or not parts[2]:
            raise URNError(f"malformed URN: {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def build(cls, kind: NodeKind | str, *segments: str) -> Self:
        """Compose a URN from a kind and scope segments.

        Segments are validated rather than escaped: a Docker id or a compose
        project name containing ``:`` or ``/`` signals that our assumptions
        about the source are wrong, and silently mangling it would produce a
        stable-looking identity that is quietly incorrect.
        """
        if not segments:
            raise URNError(f"URN of kind {kind!r} requires at least one scope segment")
        for segment in segments:
            if not segment:
                raise URNError(f"empty scope segment in {kind!r} URN")
            if _ILLEGAL_SEGMENT.search(segment):
                raise URNError(f"illegal character in scope segment {segment!r}")
        return cls(f"{NAMESPACE}:{kind}:{'/'.join(segments)}")

    @property
    def kind(self) -> str:
        return self.split(":", 2)[1]

    @property
    def scope(self) -> str:
        return self.split(":", 2)[2]

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(self.scope.split("/"))


# --------------------------------------------------------------------------
# Constructors, one per kind.
#
# These exist so the URN layout for a kind is defined in exactly one place.
# Nothing outside this module should ever build a URN by string formatting.
# --------------------------------------------------------------------------


def engine_scope(engine_id: str) -> str:
    """Normalize a Docker Engine ID into a URN-safe scope segment.

    Docker has shipped two formats for ``GET /info`` -> ``.ID``: a
    colon-delimited fingerprint (``TQ5X:PQVY:...``) on daemons before 25.0,
    and a UUID since. The colons are pure display formatting in the old
    format, so removing them is deterministic and collision-free.

    This is not the silent mangling :meth:`URN.build` refuses to do. There we
    reject unexpected separators because they signal that our model of the
    source is wrong; here we know both formats exactly.

    Without this, a single pre-25.0 host in an otherwise modern fleet would
    raise ``URNError`` and take its whole provider down -- and a mixed-version
    fleet is the normal case in the home labs this targets.
    """
    return engine_id.replace(":", "")


def host_urn(engine_id: str) -> URN:
    """Identify a host by its Docker Engine ID (``GET /info`` -> ``.ID``).

    Never an IP and never a hostname: both change under DHCP, renames and
    migrations, and a topology whose identities churn cannot keep history.
    """
    return URN.build(NodeKind.HOST, engine_id)


def container_urn(engine_id: str, container_id: str) -> URN:
    """Physical container identity. Dies when the container is recreated."""
    return URN.build(NodeKind.CONTAINER, engine_id, container_id)


def service_urn(engine_id: str, project: str, service: str) -> URN:
    """Logical service identity. Survives container recreation.

    Scoped to the engine for now. Swarm and multi-host services will widen the
    scope to a cluster id; the constructor is the single place that changes.
    """
    return URN.build(NodeKind.SERVICE, engine_id, project, service)


def stack_urn(engine_id: str, project: str) -> URN:
    return URN.build(NodeKind.STACK, engine_id, project)


def network_urn(engine_id: str, network_id: str) -> URN:
    return URN.build(NodeKind.NETWORK, engine_id, network_id)


def volume_urn(engine_id: str, volume_name: str) -> URN:
    return URN.build(NodeKind.VOLUME, engine_id, volume_name)


def image_urn(digest_or_id: str) -> URN:
    """Identify an image by content digest -- globally unique.

    Because the digest is identical on every host, the same image pulled on
    twenty hosts collapses to one node with twenty edges. Cross-host
    correlation for free, with no correlation logic at all.

    ``sha256:abc...`` becomes the segment pair ``sha256`` / ``abc...`` rather
    than being escaped, so the URN stays reversible.
    """
    algorithm, _, digest = digest_or_id.partition(":")
    if digest:
        return URN.build(NodeKind.IMAGE, algorithm, digest)
    return URN.build(NodeKind.IMAGE, algorithm)
