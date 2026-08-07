"""Transport port.

A transport's single responsibility: **make a remote engine reachable at a
local endpoint.** Nothing more. It does not know what protocol will be spoken
over it, and providers do not know which transport they are using.

That framing is what lets every planned transport fit without a core change:

===================== ========================================================
Local Unix Socket     the endpoint is already local; nothing to do
SSH Tunnel            forward a local socket to the remote engine socket
Docker TCP + mTLS     the endpoint is a URL plus a client-cert SSL context
SSH ProxyJump         same as SSH tunnel, more hops
Tailscale / WireGuard the endpoint is a URL on an overlay network
===================== ========================================================

Nothing in this module may import anything outside the standard library.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ChannelEndpoint:
    """Where a client should connect to reach the engine.

    Exactly one of ``uds_path`` or a network ``base_url`` is meaningful.
    For a Unix socket, ``base_url`` is a required-but-ignored placeholder
    hostname (``http://docker``), which is how HTTP-over-UDS is conventionally
    expressed.
    """

    base_url: str
    uds_path: str | None = None
    ssl_context: ssl.SSLContext | None = None


class TransportState(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TransportHealth:
    state: TransportState
    detail: str | None = None
    since: float = 0.0


class TransportError(RuntimeError):
    """Transport could not be established or was lost."""


@runtime_checkable
class Transport(Protocol):
    """A reusable, restartable path to one engine."""

    @property
    def id(self) -> str:
        """Stable identifier, used in logs and health reporting."""
        ...

    async def open(self) -> ChannelEndpoint:
        """Establish the path and return where to reach the engine.

        Must be idempotent: calling ``open`` on an already-open transport
        returns the existing endpoint rather than establishing a second one.
        Reconnection loops call this on every retry.
        """
        ...

    async def close(self) -> None:
        """Tear down. Must be safe to call when never opened."""
        ...

    def health(self) -> TransportHealth: ...
