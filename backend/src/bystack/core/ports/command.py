"""Command port.

The half of the product that acts. Discovery answers *what is running*;
commands are how an operator changes it, and they are the reason this is a
control plane rather than a diagram.

Three properties are load-bearing, and each of them is defended by a test:

**Commands never mutate the graph.** A ``restart`` does not optimistically
flip a node to ``restarting``. The graph changes when the provider's watch
observes the change and pushes a delta -- which means what the UI shows is
always something the infrastructure actually said, never something we
predicted it would say. Optimistic updates are how a control plane starts
lying during exactly the incidents it exists for. See ARCHITECTURE section 9.

**Read-only is enforced before dispatch, at one choke point.** Not in the
routes, not in the providers, not in the UI -- those may all *also* refuse,
but the guarantee lives in :class:`~bystack.runtime.commands.CommandService`
so that adding a second entry point (a scheduler, an agent, a webhook) cannot
accidentally bypass it.

**A target is a URN, including a logical one.** "Restart the ``web``
service" is the operation an operator actually wants; expanding it to its
containers is the Controller's job, because only the Controller holds the
graph that knows which containers realize it.

Nothing in this module may import anything outside the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from bystack.core.graph.model import Node
from bystack.core.identity import URN


class CommandKind(StrEnum):
    """Operations the platform can request.

    Restricted to **reversible lifecycle transitions**, permanently. Nothing
    here destroys state: no ``remove``, no ``prune``, no volume deletion, no
    image cleanup. That is not an oversight and not a difficulty -- they are
    each one HTTP call away.

    **This is settled, not pending.** ADR-0012 made destructive verbs
    conditional on the platform being able to answer "who deleted the database
    volume"; **ADR-0014 decided that question will not be answered** -- this is
    a single-operator LAN control plane with no user identity by design, so the
    condition is never met and the conclusion is permanent. The trade is
    deliberate and in this direction: giving up deletion in exchange for not
    needing to know who is deleting.

    It is also what keeps that safe. With no authentication and no destructive
    verb, the worst case is "someone who could reach the port restarted a
    container" -- recoverable, audited, and visible in the topology. Add
    ``prune`` and the worst case is unrecoverable data loss with no
    attribution. Adding one here is not a small change to this enum; it
    requires superseding ADR-0014 first.
    """

    START = "start"
    STOP = "stop"
    RESTART = "restart"
    PAUSE = "pause"
    UNPAUSE = "unpause"
    KILL = "kill"


class CommandStatus(StrEnum):
    """The outcome of one command against one target."""

    IN_FLIGHT = "in_flight"
    """Dispatched, not yet answered.

    Only ever seen in the audit log, and only for a command that was still
    running when the process was asked. It is what distinguishes "we never
    tried" from "we tried and never found out" -- the second of which is the
    interesting one during an incident.
    """

    SUCCEEDED = "succeeded"

    NOOP = "noop"
    """The target was already in the requested state.

    Distinct from ``succeeded`` on purpose. Docker answers "start an already
    running container" with ``304 Not Modified`` -- an unambiguous statement
    that nothing happened. Collapsing that into success would report a
    restart storm as fully effective when it changed nothing at all.
    """

    FAILED = "failed"

    TIMED_OUT = "timed_out"
    """We stopped waiting. The command may still be in flight on the host.

    Never reported as failure: a ``stop`` that exceeds its grace period is
    usually still shutting down, and the watch will tell us the truth
    shortly. Claiming failure would invite the operator to retry into an
    already-running operation.
    """

    REJECTED = "rejected"
    """Refused before anything was dispatched. The host was never contacted."""


class RejectionReason(StrEnum):
    """Why a request was refused before dispatch.

    A code rather than only prose, because the UI decides what to do with it:
    ``read_only`` disables the buttons, ``unsupported_state`` is a transient
    condition worth re-checking, ``provider_unavailable`` is a banner.
    """

    READ_ONLY = "read_only"
    UNKNOWN_TARGET = "unknown_target"
    UNSUPPORTED_TARGET = "unsupported_target"
    UNSUPPORTED_STATE = "unsupported_state"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TOO_MANY_TARGETS = "too_many_targets"


class CommandRejected(Exception):
    """A request refused before dispatch.

    Carries a machine-readable reason alongside the message, so the API layer
    can map it to a status code without parsing English.
    """

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """What an operator asked for.

    ``target`` may be physical (a container) or logical (a service, a stack).
    The service expands the logical case; nothing below it ever sees a URN it
    cannot act on directly.
    """

    kind: CommandKind
    target: URN

    timeout: float | None = None
    """Grace period in seconds for ``stop`` and ``restart``.

    ``None`` defers to the engine's own default (10s), which is the right
    answer for almost every workload and the one an operator has already
    tuned in their compose file if it is not.
    """

    signal: str | None = None
    """Signal for ``kill``. ``None`` means ``SIGKILL``, Docker's default."""

    reason: str | None = None
    """Free text recorded in the audit trail. Never interpreted."""

    actor: str = "anonymous"
    """Who asked.

    ``anonymous``, and **permanently** so: ADR-0014 decides this platform does
    not identify its operator, because at one operator on their own network
    there is nobody to distinguish them from. Recorded rather than omitted,
    because a field that plainly says "not known" is a truer audit record than
    a field that is silently absent -- or worse, one the client got to fill in.

    Not settable by the client, and that is defended by a test: an
    attacker-chosen name beside a real operation is worse than no attribution,
    because it looks like evidence.
    """


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """What happened to one concrete target."""

    target: URN
    status: CommandStatus
    detail: str | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (CommandStatus.SUCCEEDED, CommandStatus.NOOP)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The full result of one request, across every target it expanded to."""

    id: str
    kind: CommandKind
    target: URN
    actor: str
    requested_at: float
    duration_ms: int
    outcomes: tuple[TargetOutcome, ...] = ()

    @property
    def status(self) -> CommandStatus:
        """Roll the per-target outcomes into one answer.

        Worst-wins, and ``noop`` only when *every* target was already in the
        requested state. A stack restart where one of six services failed is
        a failure -- reporting it as success because the majority worked is
        how an operator walks away from a half-restarted stack.
        """
        if not self.outcomes:
            return CommandStatus.REJECTED
        for status in (
            CommandStatus.FAILED,
            CommandStatus.REJECTED,
            CommandStatus.TIMED_OUT,
        ):
            if any(o.status is status for o in self.outcomes):
                return status
        if all(o.status is CommandStatus.NOOP for o in self.outcomes):
            return CommandStatus.NOOP
        return CommandStatus.SUCCEEDED

    @property
    def ok(self) -> bool:
        return self.status in (CommandStatus.SUCCEEDED, CommandStatus.NOOP)


@runtime_checkable
class CommandExecutor(Protocol):
    """A provider's ability to act on its own partition.

    Optional: a provider that implements only :class:`Provider` is read-only
    by construction, which is the correct default for observational sources
    like Prometheus. The service checks for this protocol rather than
    assuming it, so adding a read-only provider requires writing nothing.

    Both methods are scoped to entities the provider itself discovered. The
    partition rule from ADR-0003 applies unchanged: a provider cannot be
    asked to act on something it did not report.
    """

    def supported_commands(self, node: Node) -> frozenset[CommandKind]:
        """Which commands are meaningful for this node *right now*.

        State-dependent, and deliberately so -- ``unpause`` on a running
        container is not a permission question, it is a nonsense question.
        Answering it here rather than in the UI keeps one policy in one
        place; a second copy in TypeScript would drift within a release.

        Returning an empty set is normal (an image, a network, a stopped
        container whose engine is unreachable) and never an error.
        """
        ...

    async def execute(self, request: CommandRequest, target: Node) -> TargetOutcome:
        """Perform one command against one node this provider owns.

        Must not raise for ordinary operational failure -- an unreachable
        host, a container that vanished, a refused transition are all
        outcomes, and one failing target in a stack-wide restart must not
        abort the others. Reserve exceptions for programming errors.
        """
        ...


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One durable-by-intent record of an attempted operation.

    Written **before** dispatch and finalized after, so a command that hangs
    or crashes the process still leaves evidence that it was attempted. An
    audit log that only records completions cannot answer the one question it
    is ever asked during an incident: what was tried.
    """

    id: str
    at: float
    actor: str
    kind: CommandKind
    target: URN
    targets: tuple[URN, ...] = ()
    status: CommandStatus = CommandStatus.IN_FLIGHT
    detail: str | None = None
    reason: str | None = None
    duration_ms: int = 0
    outcomes: tuple[TargetOutcome, ...] = field(default=())


@runtime_checkable
class AuditLog(Protocol):
    """Append-only record of every operation the platform attempted.

    ADR-0001 permits durable storage for exactly four categories and audit is
    one of them, with a mandatory retention policy. Two implementations honour
    both halves: `infra/audit/memory.py` is a bounded ring for a Controller
    with nowhere to write, and `infra/audit/durable.py` is the same bound on
    disk. This port is the seam that made adding the second a change to the
    composition root and nothing else -- and that will make a table a change
    to one module when Postgres lands.

    **The writes are async, and that is a property of the port rather than of
    one implementation.** The in-memory ring has nothing to await and could
    have stayed sync; making only the durable one a coroutine would put the
    choice of implementation into the shape of every caller, which is the
    opposite of what a port is for. It is also the honest signature: any
    implementation worth having beyond these two -- a file, a table, a remote
    collector -- has I/O in it, and a sync `record` forces every one of them
    to do that I/O on the event loop or silently drop it into a background
    task nobody awaits.
    """

    async def record(self, entry: AuditEntry) -> None:
        """Append. Never fails a command by failing itself.

        May await, and must not block the event loop while it does: this runs
        inside :meth:`~bystack.runtime.commands.CommandService.execute`, on
        the thread that also drives every agent's WebSocket pump and every
        browser's delta stream.
        """
        ...

    async def finalize(self, entry_id: str, entry: AuditEntry) -> None:
        """Replace a previously recorded entry with its completed form."""
        ...

    def recent(self, limit: int = 100) -> tuple[AuditEntry, ...]:
        """Most recent entries, newest first."""
        ...
