"""The door agents dial in through.

One long-lived bidirectional stream per agent, carrying protobuf frames.
Telemetry flows up and commands flow down over the same connection, and the
agent opens it -- which is the whole point of ADR-0008: managed hosts open no
port, publish no Docker socket, and need no inbound firewall rule or public
address. That is what makes the model work behind NAT, where most of the
target deployments live.

This module is transport only. It accepts a socket, decodes frames, hands them
to a provider and pushes back what the provider produces. Every decision about
what a frame *means* is in `providers/agent/`, and every decision about
*whether to listen to it at all* is in `runtime/trust.py` -- which is why the
entire ingest path is testable against a synthetic agent with no socket in
sight, and every refusal path is testable with no TLS in sight.

**Two paths, two trust levels** (ADR-0011):

- `/agents/enroll` takes no client certificate, because an agent enrolling
  does not have one. It is guarded by a single-use join token, and the token
  carries the CA fingerprint the agent checked before sending it.
- `/agents/connect` requires one, and requires its subject to be the engine id
  in `Hello`. A connection that cannot produce a certificate we issued does
  not observe anything.

**And a third door onto the same room.** `local_router` serves `connect` over
a unix socket for the agent the Controller spawned itself (`runtime/localagent
.py`, `docs/MIGRATION.md` §4). Same frames, same pump, same ingest; what
differs is one line of admission, because filesystem permissions are what
authenticate a child process to its parent. It is a separate router rather
than a flag on this one so that the mutually-authenticated listener cannot
serve it by accident -- the two are bound to different sockets by construction.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google.protobuf.message import DecodeError

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.deps import AppContext
from bystack.api.tls import peer_certificate_pem
from bystack.core.ports.agent import AgentDisconnected
from bystack.providers.agent.ingest import IngestError
from bystack.providers.agent.provider import AgentProvider
from bystack.runtime.trust import Admitted, Refusal, Refused

log = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])

#: The unix-socket listener's routes: `connect`, and nothing else.
#:
#: No enrollment, because a local agent has no certificate to be issued and
#: nothing to redeem a token for.
local_router = APIRouter(tags=["agents"])

#: Identifies this Controller process. An agent that sees a new epoch knows
#: our in-memory graph was dropped and its partition with it.
#:
#: Random per process rather than persisted, because that is exactly the
#: truth: the graph is ephemeral (ADR-0001), and a Controller that restarted
#: has genuinely forgotten everything regardless of whether its host did.
CONTROLLER_EPOCH: Final = os.urandom(8).hex()

#: Refuse frames larger than this before decoding them.
#:
#: A full Sync of a large host is the biggest legitimate frame, and a
#: thousand-container host is roughly 2 MB. The limit exists because
#: `ParseFromString` on an attacker-sized buffer is an allocation we would
#: perform on their behalf -- the input is untrusted by construction, since an
#: agent is code running on a machine we do not own.
MAX_FRAME_BYTES: Final = 16 * 1024 * 1024

#: Enrollment is one small frame from a caller holding no certificate. It gets
#: its own, far tighter bound for that reason.
MAX_ENROLL_BYTES: Final = 64 * 1024

#: Close codes. 1008 is "policy violation", which is what all of these are.
CLOSE_REFUSED: Final = 1008

#: Refusals an agent can do nothing about from where it is standing.
#:
#: Everything else is worth coming back for: an agent awaiting approval is
#: waiting on a person, and a clock that is wrong may be corrected by the NTP
#: daemon a minute from now. Getting this set wrong in the safe direction
#: costs a reconnect; getting it wrong the other way strands a host.
_TERMINAL: Final = frozenset(
    {
        Refusal.NO_CERTIFICATE,
        Refusal.UNREADABLE_CERTIFICATE,
        Refusal.IDENTITY_MISMATCH,
        Refusal.UNKNOWN,
        Refusal.REVOKED,
        Refusal.SUPERSEDED,
    }
)


class WebSocketAgentSession:
    """An :class:`~bystack.core.ports.agent.AgentSession` over a WebSocket."""

    __slots__ = (
        "_socket",
        "_engine_id",
        "_read_only",
        "_local",
        "_agent_version",
        "_capabilities",
    )

    def __init__(
        self, socket: WebSocket, engine_id: str, hello: wire.Hello, *, local: bool = False
    ) -> None:
        self._socket = socket
        # From the certificate, not from `Hello`. They have been checked
        # against each other by this point, and taking the authenticated one
        # means a future change to that check cannot quietly leave the
        # partition key sourced from the unauthenticated field.
        self._engine_id = engine_id
        self._read_only = hello.read_only
        self._local = local
        self._agent_version = hello.agent_version
        # Frozen at Hello and not re-read, because it describes the binary at
        # the other end of *this* connection. An agent upgraded in place
        # reconnects, and the new session carries the new set.
        self._capabilities = frozenset(hello.capabilities)

    @property
    def engine_id(self) -> str:
        return self._engine_id

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def local(self) -> bool:
        return self._local

    @property
    def agent_version(self) -> str:
        return self._agent_version

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    async def send(self, envelope: object) -> None:
        assert isinstance(envelope, wire.Envelope)
        try:
            await self._socket.send_bytes(envelope.SerializeToString())
        except (WebSocketDisconnect, RuntimeError) as exc:
            # RuntimeError is what Starlette raises for a socket the peer has
            # already closed. Translated at this boundary so nothing above
            # here has to know what the transport is.
            raise AgentDisconnected(f"agent {self._engine_id} is gone") from exc


# --------------------------------------------------------------------------
# Enrollment
# --------------------------------------------------------------------------


@router.websocket("/agents/enroll")
async def enroll(websocket: WebSocket) -> None:
    """Redeem a join token for one client certificate.

    A WebSocket for a single request/response, which looks odd until you count
    what the alternative costs: an HTTPS client in the agent, used once, in a
    binary whose size is a measured budget (ARCHITECTURE section 11). One
    protocol, one framing, one contract.

    No client certificate is required here and that is not a hole. The token
    is single-use and short-lived, it carries the CA fingerprint the agent
    verified before connecting, and redeeming it yields an agent that is
    `PENDING` and contributes nothing until a human approves it.
    """
    context: AppContext = websocket.app.state.context

    if not context.settings.agents.enabled:
        await websocket.close(code=CLOSE_REFUSED, reason="agent endpoint is disabled")
        return

    await websocket.accept()
    try:
        envelope = await _receive(websocket, MAX_ENROLL_BYTES)
        if envelope is None:
            return
        if envelope.WhichOneof("payload") != "enroll_request":
            await _close_quietly(websocket, CLOSE_REFUSED, "first frame must be EnrollRequest")
            return

        request = envelope.enroll_request
        outcome = context.trust.enroll(
            join_token=request.join_token,
            engine_id=request.engine_id,
            csr_pem=request.csr_pem,
            agent_version=request.agent_version,
        )
        await websocket.send_bytes(
            wire.Envelope(
                seq=1,
                enroll_response=wire.EnrollResponse(
                    accepted=outcome.accepted,
                    reason=outcome.reason,
                    certificate_pem=outcome.certificate_pem,
                    ca_pem=outcome.ca_pem,
                    not_after=outcome.not_after,
                    pending_approval=outcome.pending_approval,
                ),
            ).SerializeToString()
        )
    except WebSocketDisconnect:
        return
    finally:
        await _close_quietly(websocket, 1000, "enrollment complete")


# --------------------------------------------------------------------------
# The observation stream
# --------------------------------------------------------------------------


@router.websocket("/agents/connect")
async def connect(websocket: WebSocket) -> None:
    """Accept one mutually-authenticated agent, on the fleet's listener."""
    context: AppContext = websocket.app.state.context

    if not context.settings.agents.enabled:
        # Refused before the handshake completes. An endpoint that accepts and
        # then closes tells a scanner it exists; this one does not.
        await websocket.close(code=CLOSE_REFUSED, reason="agent endpoint is disabled")
        return
    await _serve(websocket, local=False)


@local_router.websocket("/agents/connect")
async def connect_local(websocket: WebSocket) -> None:
    """Accept the agent this Controller spawned, on its unix socket.

    Not gated on `agents.enabled`. That setting means "accept the *fleet*",
    and it is off by default because it binds a port -- neither of which is
    true here. Gating this on it would mean zero-config startup required
    opting into a listener it does not use, which is the regression this path
    exists to close.
    """
    await _serve(websocket, local=True)


async def _serve(websocket: WebSocket, *, local: bool) -> None:
    """One agent, for the lifetime of its connection.

    Below the admission line the two doors are the same code, and that is the
    requirement rather than a convenience: a local host that took a different
    ingest path would be a second implementation of discovery, maintained
    forever so that the easiest deployment could skip a subprocess.
    """
    context: AppContext = websocket.app.state.context

    await websocket.accept()

    provider: AgentProvider | None = None
    try:
        hello = await _await_hello(websocket)
        if hello is None:
            return

        verdict = (
            context.trust.admit_local(hello.engine_id)
            if local
            else context.trust.admit(
                client_certificate_pem=peer_certificate_pem(websocket.scope),
                hello_engine_id=hello.engine_id,
                agent_unix_time=hello.unix_time,
                agent_version=hello.agent_version,
            )
        )
        if isinstance(verdict, Refused):
            await _refuse(websocket, verdict)
            return

        # `create=True` is safe here and nowhere else: the engine id came out
        # of a certificate this Controller signed, for an agent the registry
        # says is approved -- or, on the local socket, out of a process this
        # Controller forked on a pipe nothing else can open. Before ADR-0011
        # this parameter was the only thing standing in front of the port,
        # which is why it is still explicit.
        provider = context.collector.agent_provider(verdict.engine_id, create=True)
        if provider is None:
            log.warning("agent %s collides with another provider", verdict.engine_id)
            await _close_quietly(websocket, CLOSE_REFUSED, "engine id is already claimed")
            return

        session = WebSocketAgentSession(websocket, verdict.engine_id, hello, local=verdict.local)
        provider.attach(session)

        # Hello is applied *after* attach, so the provider is bound before the
        # host node is written. The reverse order leaves a window in which
        # ingest writes into a partition whose provider still reports STOPPED.
        await provider.on_frame(wire.Envelope(hello=hello))
        await session.send(
            provider.hello_ack(
                epoch=CONTROLLER_EPOCH,
                resync_interval=context.settings.agents.resync_interval,
            )
        )

        # After the ack and before the pump, so the agent has its selection in
        # hand before it sends the first frame of the two host slices. The
        # other order costs a round trip of empty unit and process slices on
        # every reconnect, and the operator watches their services blink.
        #
        # **Sent even when it is empty**, which costs a frame per connection on
        # every host in the fleet and buys the one thing skipping it cannot.
        #
        # The agent answers a watch list with a full `Sync` of both host
        # slices, so an empty list is what reconciles them away. Skip it, and
        # this happens: a host is watching two services, goes offline, the
        # operator removes both, and the host reconnects. Nothing tells it, so
        # it sends no unit frame, so the slice is never reconciled -- and the
        # two cards stay on the map, for a machine that is no longer watching
        # them, until something unrelated happens to change the list again.
        #
        # The frame is a dozen bytes and the handshake already carries four
        # Sync frames. "An idle host puts zero bytes on the wire" is a claim
        # about steady state, and this is not steady state.
        #
        # A failure here is not a refusal: the list is the Controller's and it
        # is durable, so a host that could not be told is one that will be told
        # when it comes back. `send_watchlist` reports that; the connection is
        # not the place to decide what to do about it.
        await provider.send_watchlist(context.watchlist.entries(verdict.engine_id))

        if verdict.renew:
            # Over the connection that is already open and already
            # authenticated. No cron job, no second channel, no expiry outage.
            await session.send(
                wire.Envelope(
                    renewal_offer=wire.RenewalOffer(not_after=int(verdict.not_after.timestamp()))
                )
            )

        await _pump(websocket, provider, verdict, context)

    except WebSocketDisconnect:
        return
    except IngestError as exc:
        # A malformed frame from an authenticated agent is a bug in that
        # agent, not a reason to keep reading its stream: everything after it
        # is suspect, and the reconnect starts from a full Sync anyway.
        log.warning("agent %s sent an unusable frame: %s", _describe(provider), exc)
        await _close_quietly(websocket, CLOSE_REFUSED, "malformed frame")
    finally:
        if provider is not None:
            provider.detach("connection closed")


async def _refuse(websocket: WebSocket, verdict: Refused) -> None:
    """Say no, in terms the agent can act on.

    A `HelloAck` rather than a bare close, because a close code carries no
    diagnosis and this is where an operator finds out their host is pending
    approval rather than broken. `retry` is what stops "awaiting approval"
    and "revoked" being handled identically.
    """
    log.warning("refused agent connection: %s", verdict.message)
    try:
        await websocket.send_bytes(
            wire.Envelope(
                hello_ack=wire.HelloAck(
                    controller_epoch=CONTROLLER_EPOCH,
                    accepted=False,
                    reason=verdict.message,
                    retry=verdict.reason not in _TERMINAL,
                )
            ).SerializeToString()
        )
    except (WebSocketDisconnect, RuntimeError):
        return
    await _close_quietly(websocket, CLOSE_REFUSED, verdict.reason.value)


async def _await_hello(websocket: WebSocket) -> wire.Hello | None:
    """Read the first frame, which must be a Hello.

    Nothing else is accepted before it. The engine id in Hello is what selects
    the partition, so a frame arriving before it has no partition to be
    written into -- and accepting observations from a connection that has not
    identified itself is how one host's containers end up in another's graph.
    """
    envelope = await _receive(websocket, MAX_FRAME_BYTES)
    if envelope is None:
        return None
    if envelope.WhichOneof("payload") != "hello":
        await _close_quietly(websocket, CLOSE_REFUSED, "first frame must be Hello")
        return None
    if not envelope.hello.engine_id:
        await _close_quietly(websocket, CLOSE_REFUSED, "Hello carried no engine id")
        return None
    return envelope.hello


async def _pump(
    websocket: WebSocket, provider: AgentProvider, admitted: Admitted, context: AppContext
) -> None:
    """Read frames until the agent goes away."""
    while True:
        envelope = await _receive(websocket, MAX_FRAME_BYTES)
        if envelope is None:
            return
        if envelope.WhichOneof("payload") == "certificate_request":
            await _issue_renewal(websocket, admitted, context, envelope.certificate_request)
            continue
        await provider.on_frame(envelope)


async def _issue_renewal(
    websocket: WebSocket,
    admitted: Admitted,
    context: AppContext,
    request: wire.CertificateRequest,
) -> None:
    """Answer a CSR that arrived over an authenticated stream.

    The engine id comes from `admitted`, which came from the certificate --
    never from the frame. A renewal that took its subject from the request
    would be a signing oracle for any name an already-enrolled agent cared to
    ask for, which is the one thing a CA must never be.
    """
    outcome = context.trust.renew(admitted.engine_id, request.csr_pem)
    await websocket.send_bytes(
        wire.Envelope(
            certificate_issued=wire.CertificateIssued(
                ok=outcome.accepted,
                reason=outcome.reason,
                certificate_pem=outcome.certificate_pem,
                ca_pem=outcome.ca_pem,
                not_after=outcome.not_after,
            )
        ).SerializeToString()
    )


async def _receive(websocket: WebSocket, limit: int) -> wire.Envelope | None:
    """One decoded frame, or ``None`` when the connection should end.

    A protobuf decode failure closes the connection rather than skipping the
    frame. Unlike the Docker event stream -- where a malformed line is worth
    ignoring because the periodic reconcile repairs whatever it missed -- a
    frame we cannot decode here means we do not know what the agent believes,
    and the cheapest way back to a known state is a reconnect, which begins
    with a full Sync.
    """
    raw = await websocket.receive_bytes()
    if len(raw) > limit:
        await _close_quietly(websocket, CLOSE_REFUSED, "frame too large")
        return None

    envelope = wire.Envelope()
    try:
        envelope.ParseFromString(raw)
    except DecodeError as exc:
        log.warning("undecodable frame (%d bytes): %s", len(raw), exc)
        await _close_quietly(websocket, CLOSE_REFUSED, "undecodable frame")
        return None
    return envelope


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    """Close without caring whether the peer is still there."""
    try:
        await websocket.close(code=code, reason=reason)
    except (WebSocketDisconnect, RuntimeError):
        return


def _describe(provider: AgentProvider | None) -> str:
    return provider.id if provider is not None else "<unidentified>"
