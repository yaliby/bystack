"""Transport registry.

Adding a transport is a new module plus one entry here. Nothing in ``core/``,
``providers/`` or ``api/`` changes -- which is the whole point of the
abstraction and the property to protect when Tailscale, WireGuard and
TCP+mTLS arrive.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import Field

from bystack.core.ports.transport import Transport
from bystack.infra.transports.local_socket import LocalSocketConfig, LocalSocketTransport
from bystack.infra.transports.ssh_tunnel import SSHTunnelConfig, SSHTunnelTransport

#: Discriminated union of every known transport configuration. Config files
#: select an implementation by its ``type`` field and are validated against
#: that implementation's own schema.
TransportConfig = Annotated[
    LocalSocketConfig | SSHTunnelConfig,
    Field(discriminator="type"),
]

_BUILDERS: dict[str, Callable[[str, object], Transport]] = {
    "local_socket": lambda tid, cfg: LocalSocketTransport(tid, cfg),  # type: ignore[arg-type]
    "ssh_tunnel": lambda tid, cfg: SSHTunnelTransport(tid, cfg),  # type: ignore[arg-type]
}


def build_transport(transport_id: str, config: TransportConfig) -> Transport:
    """Instantiate the transport a config selects."""
    builder = _BUILDERS.get(config.type)
    if builder is None:  # pragma: no cover -- unreachable while the union is closed
        raise ValueError(f"unknown transport type {config.type!r}")
    return builder(transport_id, config)
