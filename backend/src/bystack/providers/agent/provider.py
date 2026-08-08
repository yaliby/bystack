"""Agent-backed provider: one per enrolled host.

The part of the original design that pays for itself in the pivot. `Provider`
is `id`, `kind`, `start()`, `stop()`, `health()` -- and nothing in it assumed
the provider *initiates* anything. So the port survives untouched while the
lifecycle inverts completely:

    DockerProvider   start() dials out, and the provider is the connection
    AgentProvider    exists on enrollment, and waits to be bound to one

`ProviderState` already had the right vocabulary for both, which is why
`/healthz`, the UI's status pill and the DEGRADED banner keep working with no
change at all:

    STOPPED    enrolled, never connected
    STARTING   connection accepted, awaiting Hello
    SYNCING    first Sync frames arriving
    READY      stream live
    DEGRADED   agent disconnected -- graph stale, but not wrong
    FAILED     certificate revoked or rejected

DEGRADED keeps its exact meaning. It was the right call when an SSH tunnel
flapped and it is the right call when a laptop closes its lid: the last known
topology with a clear marker beats blanking the screen.
"""

from __future__ import annotations

import logging
import time

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind
from bystack.core.ports.agent import AgentSession
from bystack.core.ports.command import (
    CommandKind,
    CommandRejected,
    CommandRequest,
    RejectionReason,
    TargetOutcome,
)
from bystack.core.ports.provider import GraphWriter, ProviderHealth, ProviderState
from bystack.providers.agent.commands import (
    CAP_LOGS,
    SUPPORTED,
    CommandChannel,
    LogsChannel,
    LogsResult,
    refuse_read_only,
)
from bystack.providers.agent.ingest import AgentIngest

# Not the cross-provider dependency ARCHITECTURE §6 forbids: that rule is
# about reading another provider's *partition*, and this is a pure function of
# a node with no state, no socket and no partition of its own. Both providers
# speak Docker's state vocabulary because the agent ships Docker's own
# vocabulary upward (§5) -- see the module docstring, which outlived the
# provider it was written beside.
from bystack.providers.docker.capability import supported_commands

log = logging.getLogger(__name__)

KIND = "agent"

#: How long to wait for an agent to answer a command.
#:
#: Larger than the Docker provider's equivalent because there is a network in
#: the middle now, and it is a network that goes through whatever NAT and home
#: uplink the managed host sits behind.
COMMAND_TIMEOUT = 45.0

#: How long to wait for an agent to answer a log read.
#:
#: Shorter than a command's deadline, and the difference is not arbitrary. A
#: command may legitimately take thirty seconds -- a graceful stop waits out
#: its own grace period at the far end -- whereas a log read is one bounded
#: response from a daemon on the same machine as the agent. An operator
#: staring at a spinner is better served by a refusal they can retry.
LOGS_TIMEOUT = 20.0


class AgentProvider:
    """One enrolled host, managed through its agent."""

    __slots__ = (
        "_id",
        "_writer",
        "_ingest",
        "_session",
        "_channel",
        "_logs",
        "_state",
        "_detail",
        "_connected_at",
        "_last_frame_at",
        "_frames",
        "_syncs",
        "_deltas",
    )

    def __init__(self, provider_id: str, writer: GraphWriter) -> None:
        self._id = provider_id
        self._writer = writer
        self._ingest = AgentIngest(provider_id, writer)
        self._session: AgentSession | None = None
        self._channel = CommandChannel()
        self._logs = LogsChannel()
        self._state = ProviderState.STOPPED
        self._detail: str | None = None
        self._connected_at = 0.0
        self._last_frame_at = 0.0
        self._frames = 0
        self._syncs = 0
        self._deltas = 0

    @property
    def id(self) -> str:
        return self._id

    @property
    def kind(self) -> str:
        return KIND

    @property
    def connected(self) -> bool:
        return self._session is not None

    @property
    def local(self) -> bool:
        """Whether the attached agent is the Controller's own child.

        Read from the live session rather than remembered, because it is a
        property of the connection: the same host can be managed locally today
        and by an enrolled remote agent tomorrow, and the answer must follow
        whichever one is actually attached.
        """
        return self._session is not None and self._session.local

    @property
    def agent_version(self) -> str:
        """What the attached agent reported, or empty when none is."""
        return self._session.agent_version if self._session is not None else ""

    @property
    def connected_at(self) -> float:
        """When the current session attached; zero when none is attached."""
        return self._connected_at if self._session is not None else 0.0

    # -- Provider ----------------------------------------------------------

    async def start(self) -> None:
        """Nothing to start.

        Deliberately not a no-op that pretends otherwise: this provider has no
        outbound work to do, and a `create_task` here would be a task waiting
        for something that arrives through an entirely different door. The
        state stays STOPPED -- "enrolled, never connected" -- until an agent
        dials in.
        """

    async def stop(self) -> None:
        """Release the connection, if any. Safe when never connected."""
        self._detach("controller shutting down")
        self._state = ProviderState.STOPPED

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            state=self._state,
            detail=self._detail,
            last_sync_at=self._last_frame_at,
            node_count=self._ingest.entity_count,
            metrics={
                "connected": self.connected,
                "syncs": self._syncs,
                "deltas": self._deltas,
                "frames": self._frames,
                "pending_commands": len(self._channel),
                "pending_logs": len(self._logs),
                "connected_for": int(time.time() - self._connected_at) if self.connected else 0,
            },
        )

    # -- session lifecycle -------------------------------------------------

    def attach(self, session: AgentSession) -> None:
        """Bind an accepted connection to this host.

        A second connection for the same host displaces the first. That is the
        correct resolution and not merely the simple one: the common cause is
        an agent whose old connection died in a way the Controller has not
        noticed yet -- a NAT rebinding, a suspended laptop -- and refusing the
        new one would leave the host unreachable until a TCP timeout we do not
        control finally fires.
        """
        if self._session is not None:
            log.info("agent %s reconnected; displacing the previous session", self._id)
            self._detach("displaced by a newer connection")

        self._session = session
        self._channel = CommandChannel()
        self._logs = LogsChannel()
        self._connected_at = time.time()
        self._set(ProviderState.STARTING, None)

    def detach(self, reason: str) -> None:
        """Unbind on disconnect. The graph is kept, and marked stale."""
        self._detach(reason)
        self._set(ProviderState.DEGRADED, reason)

    def _detach(self, reason: str) -> None:
        # Order matters: fail the waiters before dropping the session, so a
        # command in flight gets an immediate answer rather than waiting out
        # its full deadline for a reply that provably cannot arrive.
        self._channel.abandon(reason)
        self._logs.abandon(reason)
        self._session = None
        # The membership cache describes a live agent's view. Keeping it
        # across a gap would let the reconnecting agent's first Delta be
        # applied against a picture from before the disconnect, silently
        # resurrecting containers removed while it was away.
        self._ingest.reset()

    def reject(self, reason: str) -> None:
        """Refuse this agent permanently. Distinct from a disconnect.

        A revoked certificate does not improve by reconnecting, and reporting
        it as DEGRADED would have it retry forever while the UI showed a host
        that is merely "temporarily unreachable".
        """
        self._detach(reason)
        self._set(ProviderState.FAILED, reason)

    # -- frame handling ----------------------------------------------------

    async def on_frame(self, envelope: wire.Envelope) -> None:
        """Apply one decoded frame.

        The single entry point for everything an agent says. Frames that
        arrive from a session this provider is not currently bound to are
        dropped rather than applied: a displaced connection's in-flight frames
        would otherwise be written into the partition after its replacement
        had already synced.
        """
        self._frames += 1
        self._last_frame_at = time.time()

        match envelope.WhichOneof("payload"):
            case "hello":
                await self._ingest.on_hello(envelope.hello)
                self._set(ProviderState.SYNCING, None)
            case "sync":
                await self._ingest.on_sync(envelope.sync)
                self._syncs += 1
                # A Sync is a complete slice, so the partition is authoritative
                # as soon as one lands. Waiting for all four would leave the UI
                # showing SYNCING on a host whose containers are already drawn.
                self._set(ProviderState.READY, None)
            case "delta":
                await self._ingest.on_delta(envelope.delta)
                self._deltas += 1
                self._set(ProviderState.READY, None)
            case "command_result":
                self._channel.resolve(envelope.command_result)
            case "logs_response":
                self._logs.resolve(envelope.logs_response)
            case other:
                # Not an error. An agent from a later release may send frames
                # this Controller predates, and mixed-version fleets are a
                # normal operating state under ADR-0008, not a migration
                # window. Ignoring the unknown is the compatibility behaviour.
                log.debug("agent %s sent an unhandled frame %r", self._id, other)

    def hello_ack(self, *, epoch: str, resync_interval: int) -> wire.Envelope:
        return wire.Envelope(
            hello_ack=wire.HelloAck(
                controller_epoch=epoch, resync_interval=resync_interval, accepted=True
            )
        )

    async def request_resync(self, *slices: wire.Slice) -> None:
        """Ask the agent to re-List and re-hash.

        Repairs the *agent's* hash map, which after ADR-0009 is the only thing
        that can be wrong -- the Controller's graph is reconciled by every
        frame. If the hashes agree the resulting Delta is identical to a
        steady-state one and costs the same zero bytes.
        """
        session = self._session
        if session is None:
            return
        # An empty slice list is not an error: proto3 cannot distinguish it
        # from unset, and the agent reads that as "all of them", which is what
        # a caller passing no slices means.
        await session.send(
            wire.Envelope(resync_request=wire.ResyncRequest(slices=list(slices)))
        )

    # -- CommandExecutor ---------------------------------------------------

    def supported_commands(self, node: Node) -> frozenset[CommandKind]:
        """What can be done to this node through this agent, in its current state.

        This used to answer state-blind, on the grounds that a cached view of
        a host reached over someone's home uplink is a second opinion staler
        than the agent's own check (`docs/MIGRATION.md` §6.6 recorded it as a
        decision to revisit). Revisited: it is not a second opinion. ``node``
        is the very node the dashboard is drawing, so consulting it makes the
        buttons agree with the state displayed beside them — and the failure
        it removes is a card that reads `running` above a `Start` button,
        which is incoherent with itself before it is stale.

        Freshness is not the real constraint here either. The agent watches a
        local event stream and pushes within its 250ms coalescing window, and
        it still applies the authoritative check against the real daemon
        (ARCHITECTURE §9 keeps both choke points). What is gained is that an
        action guaranteed to fail is not offered; what is risked is a button
        missing for a fraction of a second after a transition, which the next
        delta repairs.

        Intersected with what this transport can carry, so a command the
        agent protocol does not implement cannot appear because Docker's
        state table permits it.
        """
        if self._session is None or node.kind != NodeKind.CONTAINER:
            return frozenset()
        if self._session.read_only:
            # Advertised at Hello, so the UI can disable the actions rather
            # than offer them and watch the agent bounce every one.
            return frozenset()
        return SUPPORTED & supported_commands(node)

    async def execute(self, request: CommandRequest, target: Node) -> TargetOutcome:
        session = self._session
        if session is None:
            raise CommandRejected(
                RejectionReason.PROVIDER_UNAVAILABLE,
                f"the agent on {self._id} is not currently connected",
            )
        if session.read_only:
            raise refuse_read_only(self._id)
        return await self._channel.dispatch(session, request, target, COMMAND_TIMEOUT)

    # -- reads -------------------------------------------------------------

    async def logs(self, container_id: str, tail: int) -> LogsResult:
        """The tail of one container's log.

        Deliberately not a `CommandKind`, and deliberately not routed through
        `CommandService`. That enum is the closed set of *mutations* a
        read-only Controller refuses; refusing to show an operator why a
        container is failing because the platform is in its safe mode would be
        exactly backwards. `session.read_only` is not consulted here for the
        same reason, and the agent answers regardless of its own setting.

        Refuses with a reason rather than an empty list, because those are
        different answers: a container that has written nothing is `ok` with
        no lines, and a host whose agent is asleep is not.
        """
        session = self._session
        if session is None:
            return LogsResult(
                False, f"the agent on {self._id} is not currently connected"
            )
        if CAP_LOGS not in session.capabilities:
            # Absence is the answer for both "too old" and "compiled out", and
            # it is named as a version so an operator knows what to upgrade.
            return LogsResult(
                False,
                f"the agent on {self._id} (version {session.agent_version or 'unknown'}) "
                f"cannot read container logs",
            )
        return await self._logs.fetch(session, container_id, tail, LOGS_TIMEOUT)

    # -- internals ---------------------------------------------------------

    def _set(self, state: ProviderState, detail: str | None) -> None:
        if self._state is not state or self._detail != detail:
            log.info("agent %s -> %s%s", self._id, state, f" ({detail})" if detail else "")
        self._state = state
        self._detail = detail
