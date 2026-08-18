"""Agent session port.

The seam between "a connection exists" and "a host is being managed".

Everything above this port -- the provider, ingest, command dispatch -- works
against an :class:`AgentSession` and never against a WebSocket. That is what
lets the entire agent path be tested against a synthetic agent with no socket,
no TLS and no Go binary anywhere, which matters because the interesting
failures (an agent that disconnects mid-command, a delta naming an id we never
saw, a reconnect that races a dispatch) are the ones no live daemon will
produce on demand.

It is also the seam ADR-0009's reversal condition needs. If the transport ever
moves to gRPC, an implementation of this protocol changes and nothing above it
does -- which is the concrete form of "the schema is the contract and the
transport is a footnote".

Nothing in this module may import anything outside the kernel.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class AgentDisconnected(ConnectionError):
    """The agent went away. Routine, not exceptional.

    A managed host closing its laptop lid, rebooting, or losing its uplink is
    the normal operating state of the fleets this targets. Callers turn this
    into DEGRADED and wait; nothing treats it as an error worth alarming on.
    """


@runtime_checkable
class AgentSession(Protocol):
    """One live connection to one agent."""

    @property
    def engine_id(self) -> str:
        """The host's Docker Engine ID, as advertised in ``Hello``.

        Under ADR-0011 this is also the agent's identity, bound into its
        client certificate -- "which host is this" and "which agent is this"
        are the same question with the same answer.
        """
        ...

    @property
    def read_only(self) -> bool:
        """Whether the agent refuses mutations regardless of what we send.

        Advertised rather than inferred, so the UI can disable actions instead
        of offering them and failing. The Controller enforces its own
        read-only setting separately; both must permit an operation for it to
        happen, and neither is a substitute for the other.
        """
        ...

    @property
    def agent_version(self) -> str:
        """What the agent said it was, in ``Hello``.

        Taken from the live connection rather than from the enrollment record,
        because an agent upgraded in place never enrols again -- a record-based
        answer would report the version the host joined with, indefinitely.
        """
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        """What this agent build can do, as advertised in ``Hello``.

        A set rather than a version comparison, because absence is the same
        answer for "too old to know the frame" and "compiled out of this
        build" -- and the caller's decision is identical in both cases. A
        version table would have to be maintained on the Controller for every
        agent release ever shipped, and would still be wrong for a build with
        a feature disabled.

        Mixed-version fleets are a normal operating state under ADR-0008, not
        a migration window, so this is consulted before sending any frame an
        older agent would not recognise -- it would otherwise be silently
        ignored at the far end and the request would time out with no
        diagnosis, which is the worst of the available failures.
        """
        ...

    @property
    def architecture(self) -> str:
        """What the host runs on, from ``Hello.engine.architecture``.

        Needed by exactly one caller and worth a property for it: a Controller
        distributing a release has to pick the artifact matching each host, and
        it is talking to a fleet that is deliberately mixed (ADR-0008). Read
        from the live connection rather than from the graph, because the answer
        must come from the same handshake as the capability that decides
        whether to send anything at all.

        Empty from an engine that did not report it, which is refused rather
        than guessed: sending an x86_64 binary to a machine that never said
        what it was is a host that goes quiet.
        """
        ...

    @property
    def local(self) -> bool:
        """Whether this agent is the one the Controller spawned itself.

        A property of the *connection*, not of the host: it says the session
        arrived on a unix socket and was admitted by filesystem permissions
        rather than by a certificate, and therefore that there is no enrollment
        record behind it and no certificate to renew. Everything else about the
        session is identical, which is why this is the only place the
        difference appears above the transport.
        """
        ...

    async def send(self, envelope: object) -> None:
        """Push one frame to the agent.

        Raises :class:`AgentDisconnected` if the connection is gone. Must not
        block indefinitely on a peer that has stopped reading -- the same
        bounded-queue rule the browser stream follows applies here in the
        other direction.
        """
        ...
