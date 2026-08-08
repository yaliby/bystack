"""Getting the peer's certificate from the socket to the route.

ASGI has a standard place for this -- ``scope["extensions"]["tls"]``, the ASGI
TLS extension -- and uvicorn does not populate it. So we do, in the smallest
subclass that can, and the route reads the standard shape rather than anything
of ours. If uvicorn grows the extension this module deletes itself and nothing
above it changes.

**Everything here fails closed.** A connection whose certificate we cannot see
is refused, not admitted with a shrug: an agent listener that cannot tell who
is calling has no business accepting observations, and "the plumbing was
missing so we allowed it" is how an authentication layer becomes decorative.
That is why `peer_certificate_pem` returns ``None`` rather than raising, and
why the only caller treats ``None`` as a refusal.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

from bystack.infra.agentca import ServerCredentials


def peer_certificate_pem(scope: Mapping[str, Any]) -> str | None:
    """The client certificate the TLS layer verified, or ``None``.

    ``None`` covers three different situations on purpose -- no TLS, no
    certificate presented, and a transport that never published one -- because
    the caller does the same thing with all three, and distinguishing them
    would invite a branch that does not.

    A certificate reaching here has already been verified against our CA by
    OpenSSL during the handshake. What is left for the application is *which*
    agent it identifies, which is `bystack.runtime.trust`'s job.
    """
    tls = scope.get("extensions", {}).get("tls")
    if not tls:
        return None
    chain = tls.get("client_cert_chain") or []
    return chain[0] if chain else None


class MutualTLSWebSocketProtocol(WebSocketsSansIOProtocol):
    """uvicorn's WebSocket protocol, plus the ASGI TLS extension.

    Only WebSocket, because the agent listener serves nothing else: enrollment
    and the observation stream are both WebSockets, deliberately, so the agent
    needs one client and one framing for everything it says (ADR-0009, and the
    binary budget in ARCHITECTURE section 11).
    """

    def connection_made(self, transport: Any) -> None:
        super().connection_made(transport)
        self._tls_extension = _tls_extension(transport)

    def handle_connect(self, event: Any) -> None:
        # `super()` builds the scope and schedules `run_asgi`. The task cannot
        # start before this callback returns -- `data_received` is a plain
        # loop callback -- so mutating the scope here happens strictly before
        # the application ever sees it.
        super().handle_connect(event)
        scope = getattr(self, "scope", None)
        if scope is None:
            # A handshake that was answered with something other than 101.
            # There is no scope and no application; nothing to annotate.
            return
        if self._tls_extension is not None:
            scope["extensions"]["tls"] = self._tls_extension


def _tls_extension(transport: Any) -> dict[str, Any] | None:
    """The ASGI TLS extension for one connection, or ``None`` on plain TCP."""
    ssl_object: ssl.SSLObject | None = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return None

    chain: list[str] = []
    peer = ssl_object.getpeercert(binary_form=True)
    if peer is not None:
        chain.append(ssl.DER_cert_to_PEM_cert(peer))

    cipher = ssl_object.cipher()
    return {
        "client_cert_chain": chain,
        "tls_version": ssl_object.version(),
        "cipher_suite": cipher[0] if cipher else None,
        "server_cert": None,
    }


def agent_ssl_context(credentials: ServerCredentials, ca_certificate: Path) -> ssl.SSLContext:
    """The listener's TLS context: our certificate, and our CA for theirs.

    ``CERT_OPTIONAL`` rather than ``CERT_REQUIRED``, and the distinction is
    smaller than it looks. OpenSSL verifies any certificate that *is*
    presented against our CA either way; the difference is only whether a
    client with none gets a handshake at all. It needs one, because an agent
    enrolling does not have a certificate yet -- that is what it is here for.

    The application, not the transport, is what requires a certificate on
    `/agents/connect`. That is the one place the requirement can be expressed
    per-path, and it is one line with a test for every way of failing it.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # No TLS 1.2. The peers are both ours, shipped together, so there is no
    # ecosystem to be compatible with and no reason to carry a decade of
    # cipher negotiation on a root-equivalent channel.
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certfile=credentials.certificate, keyfile=credentials.key)
    context.load_verify_locations(cafile=str(ca_certificate))
    context.verify_mode = ssl.CERT_OPTIONAL
    return context
