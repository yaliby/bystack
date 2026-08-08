"""Request/response correlation over an agent connection.

The `command_id` correlation map ADR-0009 chose to hand-roll rather than take
8 MB of gRPC machinery for. It is the sixty lines the ADR budgeted, and this
is the file where the bet is settled.

The shape of the problem: requests go down one stream, answers come back up
the same stream, out of order, and the caller is an `await` in an HTTP request
that must not hang forever. So each dispatch parks a future in a map keyed by
its own id, and the receive loop resolves it.

Three things must be true or this leaks, and each is handled explicitly below:
the future is always removed, a disconnect fails every waiter rather than
leaving them parked, and an answer to a question we do not remember asking is
discarded loudly rather than raising in the receive loop.

Two channels live here, and the second is deliberately *not* a command.
:class:`LogsChannel` carries reads: `CommandKind` is the closed set of
mutations a read-only Controller refuses, and refusing to show an operator why
a container is failing because the platform is in its safe mode would be
exactly backwards. Logs are their own frame, the agent answers them regardless
of `read_only`, and nothing about them goes through `CommandService`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Final

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.graph.model import Node
from bystack.core.ports.agent import AgentDisconnected, AgentSession
from bystack.core.ports.command import (
    CommandKind,
    CommandRejected,
    CommandRequest,
    CommandStatus,
    RejectionReason,
    TargetOutcome,
)

log = logging.getLogger(__name__)

#: Commands an agent-backed host accepts. Identical to the Docker provider's
#: set, because it is the same Engine API at the other end -- the agent is a
#: courier, not a different capability.
SUPPORTED: Final[frozenset[CommandKind]] = frozenset(CommandKind)

#: Argument names on the wire. Strings rather than typed fields because the
#: set is verb-specific and open; the agent ignores what it does not know.
ARG_TIMEOUT: Final = "timeout"
ARG_SIGNAL: Final = "signal"

#: The capability an agent advertises at `Hello` when it can answer a
#: `LogsRequest`. Checked before sending one: a frame an older agent does not
#: recognise is ignored at the far end, so without this the request would time
#: out with no diagnosis rather than refuse with one.
CAP_LOGS: Final = "logs"

#: The most lines an operator may ask for.
#:
#: Mirrors `MAX_LOG_LINES` in `agent/src/docker.rs`, which is where the real
#: clamp is -- this one exists so the refusal happens before a frame crosses
#: the network, and so the OpenAPI schema states the bound. The duplication is
#: intentional and the agent's copy is authoritative: it is the side holding
#: the memory budget, and it must not trust a number we sent it.
#:
#: **KNOWN GAP — `docs/OPEN-WORK.md` §3.4.** The two are kept in step by this
#: comment and nothing else. This repo already decided that cross-language
#: drift gets a guard (`test_wire.py`'s mapper/wire check); this pair has not
#: got one yet, and the failure is silent: the route would go on accepting a
#: number the agent quietly truncates.
MAX_LOG_TAIL: Final = 2000

#: What a caller gets who does not say. A screenful of scrollback and change,
#: which is what "why did this container die" actually needs.
DEFAULT_LOG_TAIL: Final = 200


class CommandChannel:
    """Outstanding commands for one agent connection.

    Bound to a connection, not to a provider: a reconnect gets a fresh
    channel, which is correct. A result arriving on a *new* connection for a
    command dispatched on the old one is not a late answer, it is an answer to
    a question the previous stream is no longer around to have asked, and
    resolving it would report the outcome of an operation whose target may
    have been rebuilt in between.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[wire.CommandResult]] = {}

    def __len__(self) -> int:
        return len(self._pending)

    async def dispatch(
        self, session: AgentSession, request: CommandRequest, target: Node, deadline: float
    ) -> TargetOutcome:
        """Send one command and wait for its result.

        ``deadline`` is how long to wait here, applied in addition to the
        command service's own. The duplication is deliberate: without it a
        disconnect between send and reply would park a future that only the
        outer deadline could free, and the entry would sit in the map until
        then holding a reference to the caller's task.

        Named ``deadline`` rather than ``timeout`` to keep it distinct from
        ``request.timeout``, which is a completely different number -- the
        grace period we hand to Docker for a graceful stop, on the far side of
        the agent.
        """
        command_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[wire.CommandResult] = loop.create_future()
        self._pending[command_id] = future

        try:
            await session.send(_command_envelope(command_id, request, target))
            async with asyncio.timeout(deadline):
                result = await future
        except AgentDisconnected as exc:
            # The host went away mid-command. Rejected rather than failed: we
            # do not know whether it ran, and reporting failure would invite a
            # retry into an operation that may have succeeded.
            return TargetOutcome(target.urn, CommandStatus.REJECTED, str(exc))
        except TimeoutError:
            return TargetOutcome(
                target.urn,
                CommandStatus.TIMED_OUT,
                f"the agent did not answer within {deadline:.0f}s; "
                f"the operation may still complete",
            )
        finally:
            # Unconditional. Every path out of here -- success, disconnect,
            # timeout, cancellation of the caller's request -- must drop the
            # entry, or a fleet under churn accumulates futures nobody will
            # ever resolve.
            self._pending.pop(command_id, None)

        return _outcome(target, result)

    def resolve(self, result: wire.CommandResult) -> None:
        """Hand a result to whoever is waiting for it.

        Called from the receive loop, so it must never raise: an unknown
        `command_id` is an agent bug or a late answer after a timeout, and
        tearing down a healthy connection over it would turn a cosmetic
        problem into an outage.
        """
        future = self._pending.get(result.command_id)
        if future is None:
            log.debug("result for unknown command %s; ignoring", result.command_id)
            return
        if not future.done():
            future.set_result(result)

    def abandon(self, reason: str) -> None:
        """Fail every waiter. Called when the connection drops.

        Without this, a disconnect leaves each in-flight command waiting out
        its full deadline for an answer that provably cannot arrive -- the
        operator watching a spinner for thirty seconds after the host has
        already gone.
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AgentDisconnected(reason))
        self._pending.clear()


@dataclass(frozen=True, slots=True)
class LogLine:
    """One line, with the stream it came out of.

    The tag is kept per line rather than flattened into one blob, because the
    line that explains a crash is almost always on stderr and that is most of
    the diagnostic value of showing logs at all.
    """

    stderr: bool
    text: str


@dataclass(frozen=True, slots=True)
class LogsResult:
    """An answer, or a refusal that says why.

    Never an empty list on its own. `GET /commands/actions` set the precedent:
    an empty answer with no reason renders as nothing at all, which an
    operator reads as a feature that failed to load rather than as an answer.
    A container that genuinely has written nothing is `ok` with no lines, and
    the UI can say so.
    """

    ok: bool
    reason: str | None = None
    lines: tuple[LogLine, ...] = ()


class LogsChannel:
    """Outstanding log reads for one agent connection.

    The same shape as :class:`CommandChannel` and for the same three reasons,
    which is why it is a sibling rather than a generalisation of it: the two
    differ in what a failure *means*. A command that times out may still have
    mutated the host, so its outcome is `REJECTED` and carries a warning
    against retrying. A log read that times out changed nothing, so it is
    simply a refusal an operator can act on by asking again.

    **KNOWN GAP — `docs/OPEN-WORK.md` §3.6.** The duplication was deliberate
    and is still ~60 copied lines: fix the leaked-future bug in one of these
    and the other keeps it. If you extract anything, extract only the parked
    future — the envelope construction and the outcome mapping are the parts
    that legitimately differ.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[wire.LogsResponse]] = {}

    def __len__(self) -> int:
        return len(self._pending)

    async def fetch(
        self, session: AgentSession, container_id: str, tail: int, deadline: float
    ) -> LogsResult:
        """Ask for the tail of one container's log, and wait for it.

        ``container_id`` is Docker's own id, never a URN -- the same line
        ADR-0009 §1 draws for commands, for the same reason: the agent does
        not know the URN scheme and must not learn it.
        """
        request_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[wire.LogsResponse] = loop.create_future()
        self._pending[request_id] = future

        try:
            await session.send(
                wire.Envelope(
                    logs_request=wire.LogsRequest(
                        request_id=request_id,
                        target_id=container_id,
                        tail=min(max(tail, 1), MAX_LOG_TAIL),
                    )
                )
            )
            async with asyncio.timeout(deadline):
                response = await future
        except AgentDisconnected as exc:
            return LogsResult(False, str(exc))
        except TimeoutError:
            return LogsResult(
                False, f"the agent did not answer within {deadline:.0f}s"
            )
        finally:
            # Unconditional, exactly as above. Every path out of here --
            # answer, disconnect, timeout, or the operator navigating away and
            # cancelling the request -- must drop the entry.
            self._pending.pop(request_id, None)

        if not response.ok:
            # The engine's own words, carried through unchanged. An operator
            # chasing a failure should not have to guess whether the container
            # or the request was wrong.
            return LogsResult(False, response.reason or "the agent could not read the log")
        return LogsResult(
            True,
            None,
            tuple(LogLine(stderr=line.stderr, text=line.text) for line in response.lines),
        )

    def resolve(self, response: wire.LogsResponse) -> None:
        """Hand an answer to whoever is waiting for it.

        Called from the receive loop, so it must never raise: an unknown
        `request_id` is a late answer after a timeout, and tearing down a
        healthy connection over it would turn a cosmetic problem into an
        outage.
        """
        future = self._pending.get(response.request_id)
        if future is None:
            log.debug("logs for unknown request %s; ignoring", response.request_id)
            return
        if not future.done():
            future.set_result(response)

    def abandon(self, reason: str) -> None:
        """Fail every waiter. Called when the connection drops."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AgentDisconnected(reason))
        self._pending.clear()


def _command_envelope(
    command_id: str, request: CommandRequest, target: Node
) -> wire.Envelope:
    """Build the frame.

    The target is Docker's own container id, taken from the URN's last
    segment. The agent does not know the URN scheme and must not learn it --
    that is the line in ADR-0009 §1, and shipping a URN down the wire would be
    the first crack in it.
    """
    args: dict[str, str] = {}
    if request.timeout is not None:
        args[ARG_TIMEOUT] = str(int(request.timeout))
    if request.signal:
        args[ARG_SIGNAL] = request.signal

    return wire.Envelope(
        command=wire.Command(
            command_id=command_id,
            verb=str(request.kind),
            target_id=target.urn.segments[-1],
            args=args,
        )
    )


def _outcome(target: Node, result: wire.CommandResult) -> TargetOutcome:
    if result.unchanged:
        # The agent saw Docker's 304. Preserved end to end rather than folded
        # into success, for the same reason the Docker provider preserves it:
        # a restart that changed nothing is not a restart that worked.
        return TargetOutcome(
            target.urn, CommandStatus.NOOP, result.detail or "already in the requested state"
        )
    if result.ok:
        return TargetOutcome(target.urn, CommandStatus.SUCCEEDED, result.detail or None)
    return TargetOutcome(
        target.urn, CommandStatus.FAILED, result.detail or "the agent reported failure"
    )


def refuse_read_only(agent_id: str) -> CommandRejected:
    """The agent itself refuses mutations, whatever we think.

    ARCHITECTURE section 9 puts the second choke point on the agent for a
    reason: it holds root-equivalent access to a machine, and "the Controller
    said so" is not an acceptable sole justification for acting on it. Because
    the agent advertises the setting at Hello, we can refuse before dispatch
    and tell the operator why, rather than sending a command we know will
    bounce.
    """
    return CommandRejected(
        RejectionReason.UNSUPPORTED_TARGET,
        f"the agent on {agent_id} is configured read-only and refuses mutations",
    )
