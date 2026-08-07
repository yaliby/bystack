"""The door agents dial in through.

One long-lived bidirectional stream per agent, carrying protobuf frames.
Telemetry flows up and commands flow down over the same connection, and the
agent opens it -- which is the whole point of ADR-0008: managed hosts open no
port, publish no Docker socket, and need no inbound firewall rule or public
address. That is what makes the model work behind NAT, where most of the
target deployments live.

This module is transport only. It accepts a socket, decodes frames, hands them
to a provider and pushes back what the provider produces. Every decision about
what a frame *means* is in `providers/agent/`, which is why the entire ingest
path is testable against a synthetic agent with no socket in sight.

**Not yet authenticated.** ADR-0011's mTLS enrollment is the next step and
until it lands this endpoint trusts whoever reaches it, so the two things
standing in front of it are that `agents.enabled` defaults to false and that
an unknown engine id is refused unless `auto_approve` is on. Both are stated
in the config docstrings, and neither is a substitute for the certificate.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google.protobuf.message import DecodeError

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.deps import AppContext
from bystack.core.ports.agent import AgentDisconnected
from bystack.providers.agent.ingest import IngestError
from bystack.providers.agent.provider import AgentProvider

log = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])

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

#: Close codes. 1008 is "policy violation", which is what both of these are.
CLOSE_REFUSED: Final = 1008


class WebSocketAgentSession:
    """An :class:`~bystack.core.ports.agent.AgentSession` over a WebSocket."""

    __slots__ = ("_socket", "_engine_id", "_read_only")

    def __init__(self, socket: WebSocket, hello: wire.Hello) -> None:
        self._socket = socket
        self._engine_id = hello.engine_id
        self._read_only = hello.read_only

    @property
    def engine_id(self) -> str:
        return self._engine_id

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def send(self, envelope: object) -> None:
        assert isinstance(envelope, wire.Envelope)
        try:
            await self._socket.send_bytes(envelope.SerializeToString())
        except (WebSocketDisconnect, RuntimeError) as exc:
            # RuntimeError is what Starlette raises for a socket the peer has
            # already closed. Translated at this boundary so nothing above
            # here has to know what the transport is.
            raise AgentDisconnected(f"agent {self._engine_id} is gone") from exc


@router.websocket("/agents/connect")
async def connect(websocket: WebSocket) -> None:
    """Accept one agent for the lifetime of its connection."""
    context: AppContext = websocket.app.state.context

    if not context.settings.agents.enabled:
        # Refused before the handshake completes. An endpoint that accepts and
        # then closes tells a scanner it exists; this one does not.
        await websocket.close(code=CLOSE_REFUSED, reason="agent endpoint is disabled")
        return

    await websocket.accept()

    provider: AgentProvider | None = None
    try:
        hello = await _await_hello(websocket)
        if hello is None:
            return

        provider = context.collector.agent_provider(
            hello.engine_id, create=context.settings.agents.auto_approve
        )
        if provider is None:
            log.warning("refused unknown agent %s", hello.engine_id)
            await websocket.close(
                code=CLOSE_REFUSED,
                reason="this agent is not enrolled; set agents.auto_approve or enroll it",
            )
            return

        session = WebSocketAgentSession(websocket, hello)
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

        await _pump(websocket, provider)

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


async def _await_hello(websocket: WebSocket) -> wire.Hello | None:
    """Read the first frame, which must be a Hello.

    Nothing else is accepted before it. The engine id in Hello is what selects
    the partition, so a frame arriving before it has no partition to be
    written into -- and accepting observations from a connection that has not
    identified itself is how one host's containers end up in another's graph.
    """
    envelope = await _receive(websocket)
    if envelope is None:
        return None
    if envelope.WhichOneof("payload") != "hello":
        await _close_quietly(websocket, CLOSE_REFUSED, "first frame must be Hello")
        return None
    if not envelope.hello.engine_id:
        await _close_quietly(websocket, CLOSE_REFUSED, "Hello carried no engine id")
        return None
    return envelope.hello


async def _pump(websocket: WebSocket, provider: AgentProvider) -> None:
    """Read frames until the agent goes away."""
    while True:
        envelope = await _receive(websocket)
        if envelope is None:
            return
        await provider.on_frame(envelope)


async def _receive(websocket: WebSocket) -> wire.Envelope | None:
    """One decoded frame, or ``None`` when the connection should end.

    A protobuf decode failure closes the connection rather than skipping the
    frame. Unlike the Docker event stream -- where a malformed line is worth
    ignoring because the periodic reconcile repairs whatever it missed -- a
    frame we cannot decode here means we do not know what the agent believes,
    and the cheapest way back to a known state is a reconnect, which begins
    with a full Sync.
    """
    raw = await websocket.receive_bytes()
    if len(raw) > MAX_FRAME_BYTES:
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
