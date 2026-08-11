"""Request/response correlation over an agent connection.

The `command_id` correlation map ADR-0009 chose to hand-roll rather than take
8 MB of gRPC machinery for. It is the sixty lines the ADR budgeted, and this
is the file where the bet is settled.

The shape of the problem: requests go down one stream, answers come back up
the same stream, out of order, and the caller is an `await` in an HTTP request
that must not hang forever. So each dispatch parks a future in a map keyed by
its own id, and the receive loop resolves it.

Three things must be true or this leaks, and each is handled once, in
:class:`ParkedRequests`: the future is always removed, a disconnect fails
every waiter rather than leaving them parked, and an answer to a question we
do not remember asking is discarded loudly rather than raising in the receive
loop.

Two channels live here, and the second is deliberately *not* a command.
:class:`LogsChannel` carries reads: `CommandKind` is the closed set of
mutations a read-only Controller refuses, and refusing to show an operator why
a container is failing because the platform is in its safe mode would be
exactly backwards. Logs are their own frame, the agent answers them regardless
of `read_only`, and nothing about them goes through `CommandService`.

**What the two share and what they do not.** The parked future is shared,
because it is the part with the bug in it -- three invariants that must hold
identically in both, and previously did only because sixty lines had been
copied accurately. Everything above it is kept separate, because the two
genuinely differ in what a failure *means*: a command that times out may
already have mutated the host, so its outcome is `REJECTED` and carries a
warning against retrying, while a log read that times out changed nothing and
is simply a refusal an operator can act on by asking again. Folding that into
a base class with a type parameter would turn a real difference into a pair of
overridden hooks and hide it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Final

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind
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

#: The capability for a *live* tail (`LogsSubscribe`). Separate from
#: `CAP_LOGS`, not implied by it: an agent that predates streaming answers
#: one-shot reads perfectly well, and the UI falls back to those rather than
#: waiting forever for a chunk that is never coming.
CAP_LOGS_STREAM: Final = "logs-stream"

#: Watching systemd units, and watching processes. Two capabilities rather than
#: one, and not implied by each other: an agent on a machine with no systemd
#: answers the process half perfectly well, and the two are separately
#: compiled and separately privileged on the host (ADR-0016). A host that
#: cannot do one of them must show the operator *which* one.
CAP_UNITS: Final = "units"
CAP_PROCESSES: Final = "processes"

#: Answering "what could I watch here". Separate again, because it is the one
#: frame that enumerates a whole machine and an agent is entitled to be built
#: without it -- a fleet whose watch lists are managed by configuration wants
#: the observation half and not the picker.
CAP_INVENTORY: Final = "inventory"

#: The most inventory rows an operator may ask for in one answer.
#:
#: Mirrors `MAX_INVENTORY_ITEMS` in `agent/src/host.rs`, which is where the
#: real clamp is, for the same reason `MAX_LOG_TAIL` mirrors `MAX_LOG_LINES`:
#: the agent holds the memory budget and must not trust a number we sent it.
#: Kept in step by `test_wire.py::test_the_constants_mirrored_across_languages_
#: still_agree`, which reads the literal out of the Rust source.
MAX_INVENTORY: Final = 500

#: What a caller gets who does not say. A picker shows a scrolling list and a
#: filter box; two hundred rows is more than anyone reads before typing.
DEFAULT_INVENTORY: Final = 200

#: Lines a single live subscription will hold for a reader that is behind.
#:
#: The one place backpressure has to be decided, so it is decided here and
#: named. A browser on a slow link watching a container in a hot loop cannot
#: be allowed to grow this process's memory, and it cannot be allowed to slow
#: the agent's WebSocket pump either -- that pump carries every other host's
#: topology. So the queue is bounded and **the oldest lines are dropped**,
#: which is the right loss for a live tail: an operator watching output scroll
#: wants the newest lines, and a tail that stalled to preserve history nobody
#: asked for would be the wrong feature. The drop is reported, not silent.
STREAM_QUEUE: Final = 500

#: The most lines an operator may ask for.
#:
#: Mirrors `MAX_LOG_LINES` in `agent/src/docker.rs`, which is where the real
#: clamp is -- this one exists so the refusal happens before a frame crosses
#: the network, and so the OpenAPI schema states the bound. The duplication is
#: intentional and the agent's copy is authoritative: it is the side holding
#: the memory budget, and it must not trust a number we sent it.
#:
#: Kept in step by a guard rather than by this comment:
#: `test_wire.py::test_the_constants_mirrored_across_languages_still_agree`
#: reads the literal out of the Rust source. Without it the drift is silent --
#: the route goes on advertising a bound it no longer has and accepting a
#: number the agent quietly truncates.
MAX_LOG_TAIL: Final = 2000

#: What a caller gets who does not say, and what the UI takes: the client
#: omits `tail` rather than keeping a third copy of this number in TypeScript.
#: A screenful of scrollback and change, which is what "why did this container
#: die" actually needs.
DEFAULT_LOG_TAIL: Final = 200


class ParkedRequests[T]:
    """Futures waiting for an answer over one agent connection.

    The correlation map itself, and nothing about what is being correlated.
    Both channels are built on it because all three of the ways this leaks are
    properties of the map rather than of the payload, and a copy of them is a
    copy of the bug they prevent:

    - the future is dropped on **every** path out of :meth:`request`, including
      the caller's own cancellation;
    - :meth:`abandon` fails every waiter when the connection drops, so nobody
      sits out a deadline for an answer that provably cannot arrive;
    - :meth:`resolve` never raises, because it is called from the receive loop
      and tearing down a healthy connection over a late answer would turn a
      cosmetic problem into an outage.

    Bound to a connection, not to a provider: a reconnect gets a fresh map,
    which is correct. An answer arriving on a *new* connection for a request
    made on the old one is not a late answer, it is an answer to a question
    the previous stream is no longer around to have asked -- and for a command
    that means reporting the outcome of an operation whose target may have
    been rebuilt in between.
    """

    __slots__ = ("_what", "_pending")

    def __init__(self, what: str) -> None:
        #: What these are, for the one log line that names them. The channels
        #: differ in what an unknown id *means* -- an agent bug for a command,
        #: an ordinary late answer for a log read -- so the word is worth
        #: keeping in the message.
        self._what = what
        self._pending: dict[str, asyncio.Future[T]] = {}

    def __len__(self) -> int:
        return len(self._pending)

    async def request(
        self, session: AgentSession, request_id: str, envelope: wire.Envelope, deadline: float
    ) -> T:
        """Park a future, send the frame, and wait for the answer.

        Raises :class:`AgentDisconnected` if the host goes away and
        ``TimeoutError`` if the deadline passes. Both are left to the caller
        rather than mapped here: what a failure *means* is the part the two
        channels do not share.

        ``deadline`` is how long to wait here, applied in addition to any the
        caller already has. The duplication is deliberate: without it a
        disconnect between send and reply would park a future that only the
        outer deadline could free, and the entry would sit in the map until
        then holding a reference to the caller's task.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._pending[request_id] = future
        try:
            await session.send(envelope)
            async with asyncio.timeout(deadline):
                return await future
        finally:
            # Unconditional. Every path out of here -- answer, disconnect,
            # timeout, cancellation of the caller's request -- must drop the
            # entry, or a fleet under churn accumulates futures nobody will
            # ever resolve.
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, answer: T) -> None:
        """Hand an answer to whoever is waiting for it. Never raises."""
        future = self._pending.get(request_id)
        if future is None:
            log.debug("%s for unknown request %s; ignoring", self._what, request_id)
            return
        if not future.done():
            future.set_result(answer)

    def abandon(self, reason: str) -> None:
        """Fail every waiter. Called when the connection drops.

        Without this, a disconnect leaves each in-flight request waiting out
        its full deadline for an answer that provably cannot arrive -- the
        operator watching a spinner for thirty seconds after the host has
        already gone.
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AgentDisconnected(reason))
        self._pending.clear()


class CommandChannel:
    """Outstanding commands for one agent connection.

    Owns the envelope and the outcome mapping; the parked future is
    :class:`ParkedRequests`.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: ParkedRequests[wire.CommandResult] = ParkedRequests("result")

    def __len__(self) -> int:
        return len(self._pending)

    async def dispatch(
        self, session: AgentSession, request: CommandRequest, target: Node, deadline: float
    ) -> TargetOutcome:
        """Send one command and wait for its result.

        ``deadline`` is named that rather than ``timeout`` to keep it distinct
        from ``request.timeout``, which is a completely different number --
        the grace period we hand to Docker for a graceful stop, on the far
        side of the agent.
        """
        command_id = uuid.uuid4().hex[:16]
        try:
            result = await self._pending.request(
                session,
                command_id,
                _command_envelope(command_id, request, target),
                deadline,
            )
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
        return _outcome(target, result)

    def resolve(self, result: wire.CommandResult) -> None:
        """Hand a result to whoever is waiting for it."""
        self._pending.resolve(result.command_id, result)

    def abandon(self, reason: str) -> None:
        """Fail every waiter. Called when the connection drops."""
        self._pending.abandon(reason)


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

    A sibling of :class:`CommandChannel` rather than a subclass of it, over
    the same :class:`ParkedRequests`. The two differ in exactly the places
    that are left here: a command that times out may still have mutated the
    host, so its outcome is `REJECTED` and carries a warning against retrying,
    while a log read that times out changed nothing and is simply a refusal an
    operator can act on by asking again.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: ParkedRequests[wire.LogsResponse] = ParkedRequests("logs")

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
        try:
            response = await self._pending.request(
                session,
                request_id,
                wire.Envelope(
                    logs_request=wire.LogsRequest(
                        request_id=request_id,
                        target_id=container_id,
                        tail=min(max(tail, 1), MAX_LOG_TAIL),
                    )
                ),
                deadline,
            )
        except AgentDisconnected as exc:
            return LogsResult(False, str(exc))
        except TimeoutError:
            return LogsResult(False, f"the agent did not answer within {deadline:.0f}s")

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
        """Hand an answer to whoever is waiting for it."""
        self._pending.resolve(response.request_id, response)

    def abandon(self, reason: str) -> None:
        """Fail every waiter. Called when the connection drops."""
        self._pending.abandon(reason)


@dataclass(frozen=True, slots=True)
class InventoryItem:
    """One row in the picker.

    Deliberately thin. This is a menu of things that *could* be watched, not an
    observation of them: carrying a full unit payload for four hundred units so
    that a list of names can be drawn would put the cost of watching everything
    back on a host that has chosen to watch three things.
    """

    id: str
    name: str
    description: str = ""
    state: str = ""
    detail: str = ""
    pid: int = 0


@dataclass(frozen=True, slots=True)
class InventoryResult:
    """An answer, or a refusal that says why.

    Same shape and same argument as :class:`LogsResult`: an empty list with no
    reason renders as nothing at all, which an operator reads as a feature that
    failed to load rather than as a machine with no matching units.
    """

    ok: bool
    reason: str | None = None
    items: tuple[InventoryItem, ...] = ()
    total: int = 0
    """Matches before the agent's cap, so the UI can say "200 of 412". An
    operator whose service is number three hundred would otherwise conclude it
    is not installed."""


class InventoryChannel:
    """Outstanding inventory reads for one agent connection.

    The third user of :class:`ParkedRequests`, and a read like the second: it
    enumerates and changes nothing, so a read-only Controller answers it
    through a read-only agent. `CommandKind` is the closed set of mutations,
    and asking a machine what it is running is not one.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: ParkedRequests[wire.InventoryResponse] = ParkedRequests("inventory")

    def __len__(self) -> int:
        return len(self._pending)

    async def fetch(
        self, session: AgentSession, kind: str, filter_text: str, limit: int, deadline: float
    ) -> InventoryResult:
        request_id = uuid.uuid4().hex[:16]
        try:
            response = await self._pending.request(
                session,
                request_id,
                wire.Envelope(
                    inventory_request=wire.InventoryRequest(
                        request_id=request_id,
                        kind=kind,
                        filter=filter_text,
                        limit=min(max(limit, 1), MAX_INVENTORY),
                    )
                ),
                deadline,
            )
        except AgentDisconnected as exc:
            return InventoryResult(False, str(exc))
        except TimeoutError:
            # Unlike a command, this changed nothing, so saying so plainly and
            # letting the operator ask again is the whole of the right answer.
            return InventoryResult(False, f"the agent did not answer within {deadline:.0f}s")

        if not response.ok:
            return InventoryResult(False, response.reason or "the agent could not enumerate")
        return InventoryResult(
            True,
            None,
            tuple(
                InventoryItem(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    state=item.state,
                    detail=item.detail,
                    pid=item.pid,
                )
                for item in response.items
            ),
            response.total,
        )

    def resolve(self, response: wire.InventoryResponse) -> None:
        self._pending.resolve(response.request_id, response)

    def abandon(self, reason: str) -> None:
        self._pending.abandon(reason)


@dataclass(frozen=True, slots=True)
class LogsEvent:
    """One thing that happened on a live tail.

    Either lines, or the end. `done` with a `reason` is a failure; `done`
    without one is the log ending normally -- the container exited, or the
    engine closed the stream. The operator needs those distinguished: silence
    after a crash and silence after a clean stop look identical otherwise.
    """

    lines: tuple[LogLine, ...] = ()
    done: bool = False
    reason: str | None = None
    dropped: int = 0
    """Lines discarded because this reader could not keep up.

    Reported rather than hidden. A gap in a log that the UI does not mark is a
    log an operator will read as continuous, and they will draw a conclusion
    from two adjacent lines that were never adjacent.
    """


class LogsUnavailable(Exception):
    """A live tail could not be opened, with a reason worth showing.

    Raised rather than returned because the caller is a streaming response
    that has not begun: it can still answer with a status code and a body. Once
    the first chunk is on the wire that option is gone, which is why every
    reason this can fail is checked before the subscription is made.
    """


class LogsStream:
    """One live subscription, from the Controller's side.

    An `asyncio.Queue` with a bound and a drop policy, plus the id the agent
    knows it by. Deliberately *not* a `ParkedRequests` entry: that class is
    built for one answer to one question, and everything about it -- the
    future, the deadline, the unconditional removal in `finally` -- is wrong
    for a thing that produces many answers over an unbounded time.
    """

    __slots__ = ("request_id", "container_id", "_queue", "_dropped", "_closed")

    def __init__(self, request_id: str, container_id: str) -> None:
        self.request_id = request_id
        self.container_id = container_id
        self._queue: asyncio.Queue[LogsEvent] = asyncio.Queue(maxsize=STREAM_QUEUE)
        self._dropped = 0
        self._closed = False

    def offer(self, event: LogsEvent) -> None:
        """Hand an event to the reader. Never blocks, never raises.

        Called from the receive loop, which must not be slowed by a browser on
        a bad connection -- it is the same loop carrying every other host's
        deltas. When the queue is full the *oldest* line is dropped and the
        count travels with the next event, so the UI can say "42 lines
        dropped" instead of quietly showing a discontinuous log.

        The terminal event is never dropped. A reader that missed it would
        wait forever for a stream that has already ended.
        """
        if self._closed:
            return
        if event.done:
            # Set before the put, not after: anything still arriving behind
            # the end is then ignored rather than queued after it.
            self._closed = True

        # One slot, made unconditionally, which is what makes the terminal
        # event safe as well as the ordinary ones -- a bounded queue needs room
        # for exactly one and this is where it comes from. There was a second,
        # forcing loop above this for the `done` case; it discarded the same
        # single event this does and could never fire twice, so it was doing
        # nothing but implying the guarantee lived somewhere other than here.
        if self._queue.full():
            _discard(self._queue)
            self._dropped += 1

        # The count rides out on the next event of any kind, terminal
        # included: a stream that ended having lost lines must say so, or the
        # operator reads the last thing they saw as the last thing written.
        if self._dropped:
            event = replace(event, dropped=self._dropped)
            self._dropped = 0

        self._queue.put_nowait(event)

    async def next(self) -> LogsEvent:
        """The next event, waiting if there is none yet."""
        return await self._queue.get()

    def close(self, reason: str) -> None:
        """End the stream from this side. Idempotent."""
        if not self._closed:
            self.offer(LogsEvent(done=True, reason=reason))


def _discard(queue: asyncio.Queue[LogsEvent]) -> None:
    """Drop the oldest event. A no-op on an empty queue, which cannot happen
    from the call sites above but is not worth an exception if it ever does."""
    with contextlib.suppress(asyncio.QueueEmpty):
        queue.get_nowait()


class LogsSubscriptions:
    """Live tails open on one agent connection.

    The streaming counterpart of :class:`ParkedRequests`, and a separate class
    for the reason above: same problem shape, different lifetime. What it does
    share is the property that matters -- everything here is bound to one
    connection, and `abandon` ends every stream when that connection drops, so
    no reader is left waiting on a host that has gone away.
    """

    __slots__ = ("_streams",)

    def __init__(self) -> None:
        self._streams: dict[str, LogsStream] = {}

    def __len__(self) -> int:
        return len(self._streams)

    def open(self, container_id: str) -> LogsStream:
        request_id = uuid.uuid4().hex[:16]
        stream = LogsStream(request_id, container_id)
        self._streams[request_id] = stream
        return stream

    def close(self, request_id: str) -> None:
        self._streams.pop(request_id, None)

    def deliver(self, chunk: wire.LogsChunk) -> None:
        """Route one chunk to its reader. Called from the receive loop.

        Never raises: an unknown `request_id` is an in-flight chunk arriving
        after a cancel, which is ordinary, and tearing down a healthy
        connection over it would turn a race into an outage.
        """
        stream = self._streams.get(chunk.request_id)
        if stream is None:
            log.debug("log chunk for unknown subscription %s; ignoring", chunk.request_id)
            return
        stream.offer(
            LogsEvent(
                lines=tuple(
                    LogLine(stderr=line.stderr, text=line.text) for line in chunk.lines
                ),
                done=chunk.done,
                reason=chunk.reason or None,
            )
        )
        if chunk.done:
            self._streams.pop(chunk.request_id, None)

    def abandon(self, reason: str) -> None:
        """End every stream. Called when the connection drops."""
        for stream in list(self._streams.values()):
            stream.close(reason)
        self._streams.clear()


def _command_envelope(
    command_id: str, request: CommandRequest, target: Node
) -> wire.Envelope:
    """Build the frame.

    The target is the id in the vocabulary of whatever owns it -- a container
    id, a unit name, or the watch id of a process rule -- taken from the URN's
    last segment, which is where all three of them live. The agent does not
    know the URN scheme and must not learn it: that is the line in ADR-0009 §1,
    and shipping a URN down the wire would be the first crack in it.

    `target_kind` is what keeps the three apart at the far end. The agent must
    not infer it from the shape of the string: `docker`-prefixed unit names
    exist, container ids are hexadecimal and so are plenty of other things, and
    an agent that guessed wrong would send a lifecycle command to the wrong
    subsystem entirely. Empty means `container`, which is what every Controller
    before the host slices sent -- so an old frame and a new agent agree
    without a version check.
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
            target_kind=TARGET_KIND.get(target.kind, ""),
            args=args,
        )
    )


#: Node kind -> the `target_kind` the agent reads. Absent for a container,
#: because absent *is* container on the wire and adding a spelling for it would
#: mean two Controllers could disagree about which one an old agent accepts.
TARGET_KIND: Final[dict[str, str]] = {
    NodeKind.UNIT: "unit",
    NodeKind.PROCESS: "process",
}


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
