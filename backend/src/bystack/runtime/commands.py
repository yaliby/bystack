"""The command service: authorize, expand, audit, dispatch, return.

The single choke point through which every operation passes, from any entry
point that will ever exist -- the REST API today, a scheduler, a webhook or
an alert-driven remediation later. That is the whole reason it is a service
and not four lines in a route handler: a guarantee that lives in one route is
not a guarantee, it is a habit.

The pipeline, in order, and the order matters:

    authorize  -> read-only refuses here, before anything is resolved
    resolve    -> URN to node, node to the provider that owns it
    expand     -> a service or stack becomes the containers that realize it
    audit      -> written before dispatch, so a hang still leaves evidence
    dispatch   -> bounded concurrency, per-target deadline
    return     -> per-target outcomes; the graph is not touched

**The graph is not touched.** Not before, not after. A ``restart`` returns
without a single node having changed, and the UI keeps showing ``running``
until the provider's watch observes the actual transition and pushes a delta.
This looks like a missing feature for about ten seconds and then stops
looking like one forever: it is the reason the topology can never show a
state the infrastructure did not report. Optimistic updates are how a control
plane starts lying during exactly the incident it exists for.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

from bystack.core.graph.model import EdgeKind, Node
from bystack.core.graph.store import GraphStore
from bystack.core.identity import URN, NodeKind
from bystack.core.ports.command import (
    AuditEntry,
    AuditLog,
    CommandExecutor,
    CommandKind,
    CommandRejected,
    CommandRequest,
    CommandResult,
    CommandStatus,
    RejectionReason,
    TargetOutcome,
)
from bystack.core.ports.provider import Provider

log = logging.getLogger(__name__)

#: How many targets one request may fan out to.
#:
#: A guard against the operator who selects a stack of two hundred containers
#: and restarts it, not against malice. The limit refuses the whole request
#: rather than truncating it: a partially-applied stack restart is the single
#: worst outcome available here, and silently doing half of what was asked is
#: how you get one.
MAX_TARGETS: Final = 64

#: Targets dispatched at once. A stack restart should be concurrent -- serial
#: would take a minute for something the engine can do in seconds -- but a
#: hundred simultaneous requests to one daemon is a denial of service we
#: inflicted on the host we are supposed to be operating.
MAX_CONCURRENCY: Final = 8

#: The engine's own default stop grace period, per the Docker API docs. Used
#: only to compute our deadline; we never send it, so a container with its own
#: configured grace keeps it.
ENGINE_DEFAULT_GRACE: Final = 10.0

#: Headroom above the grace period before we stop waiting. Covers the round
#: trip, the tunnel and the daemon's own bookkeeping.
DISPATCH_MARGIN: Final = 20.0

#: Deadline for commands that have no grace period of their own.
DEFAULT_DEADLINE: Final = 30.0

#: Presentation order for available actions. Least destructive first, so the
#: dangerous one is never where the mouse already is.
_ORDER: Final[tuple[CommandKind, ...]] = (
    CommandKind.START,
    CommandKind.RESTART,
    CommandKind.UNPAUSE,
    CommandKind.PAUSE,
    CommandKind.STOP,
    CommandKind.KILL,
)


@dataclass(frozen=True, slots=True)
class AvailableActions:
    """What an operator may do to a target, and why not, when nothing."""

    target: URN
    kind: str
    targets: tuple[URN, ...]
    """The concrete containers this target expands to. Shown in the UI so
    "restart" on a stack states plainly how many things it will restart."""

    commands: tuple[CommandKind, ...] = ()
    reason: RejectionReason | None = None
    detail: str | None = None


class CommandService:
    """Authorizes and dispatches operations against discovered infrastructure."""

    __slots__ = ("_store", "_audit", "_lookup", "_read_only", "_max_targets")

    def __init__(
        self,
        store: GraphStore,
        audit: AuditLog,
        provider_lookup: Callable[[str], Provider | None],
        *,
        read_only: bool = True,
        max_targets: int = MAX_TARGETS,
    ) -> None:
        self._store = store
        self._audit = audit
        # A lookup rather than the collector itself: this service needs to
        # find one provider by id and nothing else, and depending on the
        # whole supervisor for that would make it untestable without one.
        self._lookup = provider_lookup
        self._read_only = read_only
        self._max_targets = max_targets

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def audit(self) -> AuditLog:
        return self._audit

    # -- inspection --------------------------------------------------------

    def actions(self, target: URN) -> AvailableActions:
        """What can be done to ``target`` right now.

        Answered here rather than in the UI so that the policy exists once. A
        copy of "unpause applies to paused containers" in TypeScript would be
        correct on the day it was written and wrong by the release that adds
        a state.

        For a multi-target (a stack, a service with replicas) the answer is
        the **union** across its containers, not the intersection. A stack
        with one crashed container out of six should still offer ``start`` --
        that is the operator's whole reason for being there -- and offering
        only what applies uniformly would hide the action precisely when it
        is needed.
        """
        node = self._store.node(target)
        if node is None:
            return AvailableActions(
                target=target,
                kind="",
                targets=(),
                reason=RejectionReason.UNKNOWN_TARGET,
                detail=f"no such node: {target}",
            )

        try:
            targets = self._expand(node)
        except CommandRejected as exc:
            return AvailableActions(
                target=target, kind=node.kind, targets=(), reason=exc.reason, detail=exc.detail
            )

        urns = tuple(t.urn for t in targets)

        if self._read_only:
            # Reported as an available-actions answer rather than an empty
            # list, so the UI can explain *why* the buttons are absent. An
            # unexplained absence reads as a broken page.
            return AvailableActions(
                target=target,
                kind=node.kind,
                targets=urns,
                reason=RejectionReason.READ_ONLY,
                detail="the control plane is running read-only; set read_only: false to operate",
            )

        available: set[CommandKind] = set()
        reachable = False
        for candidate in targets:
            executor = self._executor_for(candidate)
            if executor is not None:
                reachable = True
                available |= executor.supported_commands(candidate)

        if not available:
            # An empty list with no reason renders as nothing at all, which
            # reads as a feature that failed to load. Name which of the two
            # emptinesses this is: nobody to ask, or nothing to ask for.
            return AvailableActions(
                target=target,
                kind=node.kind,
                targets=urns,
                reason=(
                    RejectionReason.UNSUPPORTED_STATE
                    if reachable
                    else RejectionReason.PROVIDER_UNAVAILABLE
                ),
                detail=(
                    f"nothing can be done to a container that is {node.status!r}"
                    if reachable
                    else "no connected agent owns this host right now"
                ),
            )

        return AvailableActions(
            target=target,
            kind=node.kind,
            targets=urns,
            commands=tuple(k for k in _ORDER if k in available),
        )

    # -- execution ---------------------------------------------------------

    async def execute(self, request: CommandRequest) -> CommandResult:
        """Run a command. Raises :class:`CommandRejected` if refused."""
        command_id = uuid.uuid4().hex[:16]
        requested_at = time.time()
        started = time.perf_counter()

        try:
            targets = self._authorize(request)
        except CommandRejected as exc:
            # Refusals are audited too, and this is not box-ticking: "the
            # restart did not happen because the platform is read-only" is
            # the single most useful line the log can contain during the
            # post-mortem of an outage that a restart would have ended.
            await self._audit.record(
                AuditEntry(
                    id=command_id,
                    at=requested_at,
                    actor=request.actor,
                    kind=request.kind,
                    target=request.target,
                    status=CommandStatus.REJECTED,
                    detail=f"{exc.reason}: {exc.detail}",
                    reason=request.reason,
                )
            )
            raise

        pending = AuditEntry(
            id=command_id,
            at=requested_at,
            actor=request.actor,
            kind=request.kind,
            target=request.target,
            targets=tuple(t.urn for t in targets),
            status=CommandStatus.IN_FLIGHT,
            reason=request.reason,
        )
        await self._audit.record(pending)

        outcomes = await self._dispatch(request, targets)
        duration_ms = int((time.perf_counter() - started) * 1000)

        result = CommandResult(
            id=command_id,
            kind=request.kind,
            target=request.target,
            actor=request.actor,
            requested_at=requested_at,
            duration_ms=duration_ms,
            outcomes=outcomes,
        )

        await self._audit.finalize(
            command_id,
            AuditEntry(
                id=command_id,
                at=requested_at,
                actor=request.actor,
                kind=request.kind,
                target=request.target,
                targets=tuple(t.urn for t in targets),
                status=result.status,
                detail=_summarize(outcomes),
                reason=request.reason,
                duration_ms=duration_ms,
                outcomes=outcomes,
            ),
        )

        log.info(
            "command %s %s on %s by %s -> %s (%d target(s), %dms)",
            command_id, request.kind, request.target, request.actor,
            result.status, len(outcomes), duration_ms,
        )
        return result

    # -- pipeline stages ---------------------------------------------------

    def _authorize(self, request: CommandRequest) -> tuple[Node, ...]:
        """Every reason to refuse, in the order that costs least to check."""
        if self._read_only:
            # First, and before the target is even resolved. A read-only
            # control plane must not be usable as an oracle for which URNs
            # exist, and more practically: this is the check that must be
            # impossible to reach past, so nothing runs before it.
            raise CommandRejected(
                RejectionReason.READ_ONLY,
                "the control plane is running read-only; mutation is opt-in",
            )

        node = self._store.node(request.target)
        if node is None:
            raise CommandRejected(
                RejectionReason.UNKNOWN_TARGET, f"no such node: {request.target}"
            )

        targets = self._expand(node)
        if not targets:
            raise CommandRejected(
                RejectionReason.UNSUPPORTED_TARGET,
                f"{node.kind} {node.name!r} has no running containers to act on",
            )
        if len(targets) > self._max_targets:
            raise CommandRejected(
                RejectionReason.TOO_MANY_TARGETS,
                f"{len(targets)} targets exceeds the limit of {self._max_targets}; "
                f"act on individual services instead",
            )

        for target in targets:
            if self._executor_for(target) is None:
                raise CommandRejected(
                    RejectionReason.UNSUPPORTED_TARGET,
                    f"the provider that discovered {target.name!r} cannot execute commands",
                )
        return targets

    def _expand(self, node: Node) -> tuple[Node, ...]:
        """Resolve a target to the concrete containers it means.

        This is the operation that makes logical URNs useful. An operator
        wants to restart *the web service*, not to look up which of three
        replica containers currently realizes it -- and only the Controller
        holds the graph that can answer that.

        Traversal follows the same edges the mapper declared, so it stays
        correct for free as the model grows: ``stack -CONTAINS-> service
        -REALIZED_BY-> container``.
        """
        match node.kind:
            case NodeKind.CONTAINER:
                return (node,)
            case NodeKind.SERVICE:
                return self._follow(node.urn, EdgeKind.REALIZED_BY, NodeKind.CONTAINER)
            case NodeKind.STACK:
                found: dict[URN, Node] = {}
                for service in self._follow(node.urn, EdgeKind.CONTAINS, NodeKind.SERVICE):
                    for container in self._follow(
                        service.urn, EdgeKind.REALIZED_BY, NodeKind.CONTAINER
                    ):
                        found[container.urn] = container
                return tuple(sorted(found.values(), key=lambda n: n.name))
            case _:
                raise CommandRejected(
                    RejectionReason.UNSUPPORTED_TARGET,
                    f"{node.kind} nodes cannot be operated on; "
                    f"target a container, service or stack",
                )

    def _follow(self, src: URN, kind: EdgeKind, dst_kind: NodeKind) -> tuple[Node, ...]:
        """Outgoing edges of one kind, resolved to nodes, name-ordered.

        Direction is checked explicitly. :meth:`GraphStore.neighbors` returns
        edges incident in *either* direction, and treating an incoming
        ``REALIZED_BY`` as an outgoing one would have a container resolve to
        its own service and then back again.
        """
        nodes: list[Node] = []
        for edge in self._store.neighbors(src):
            if edge.kind is not kind or edge.src != src:
                continue
            node = self._store.node(edge.dst)
            if node is not None and node.kind == dst_kind:
                nodes.append(node)
        return tuple(sorted(nodes, key=lambda n: n.name))

    async def _dispatch(
        self, request: CommandRequest, targets: Iterable[Node]
    ) -> tuple[TargetOutcome, ...]:
        """Run every target concurrently, under a bound and a deadline.

        No target can fail another. One unreachable container in a stack
        restart produces one failed outcome and five successful ones, which
        is both the truth and the only report an operator can act on.
        """
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        deadline = _deadline(request)

        async def run(target: Node) -> TargetOutcome:
            async with semaphore:
                started = time.perf_counter()
                executor = self._executor_for(target)
                if executor is None:  # pragma: no cover - _authorize checked this
                    return TargetOutcome(
                        target.urn, CommandStatus.REJECTED, "provider cannot execute commands"
                    )
                try:
                    async with asyncio.timeout(deadline):
                        return await executor.execute(request, target)
                except TimeoutError:
                    # Not a failure. A stop that outlives its grace period is
                    # usually still shutting down, and the watch will report
                    # the truth within seconds. Calling it failed would
                    # invite a retry into an operation already in flight.
                    return TargetOutcome(
                        target.urn,
                        CommandStatus.TIMED_OUT,
                        f"no answer within {deadline:.0f}s; the operation may still complete",
                        int((time.perf_counter() - started) * 1000),
                    )
                except CommandRejected as exc:
                    return TargetOutcome(
                        target.urn,
                        CommandStatus.REJECTED,
                        exc.detail,
                        int((time.perf_counter() - started) * 1000),
                    )
                except Exception as exc:
                    # A provider defect, not an operational failure. Logged
                    # loudly and contained: it must not take down the other
                    # targets of the same request.
                    log.exception("provider raised executing %s on %s", request.kind, target.urn)
                    return TargetOutcome(
                        target.urn,
                        CommandStatus.FAILED,
                        f"internal error: {exc!r}",
                        int((time.perf_counter() - started) * 1000),
                    )

        return tuple(await asyncio.gather(*(run(t) for t in targets)))

    def _executor_for(self, node: Node) -> CommandExecutor | None:
        """The provider that owns this node, if it can act at all.

        ``node.source`` is the partition key, so this is also the enforcement
        of ADR-0003 for commands: a node can only ever be operated on by the
        provider that discovered it. There is no path by which one provider
        is asked to act on another's entity.
        """
        provider = self._lookup(node.source)
        if provider is None:
            return None
        return provider if isinstance(provider, CommandExecutor) else None


def _deadline(request: CommandRequest) -> float:
    """How long to wait before giving up on one target.

    Derived from the grace period rather than fixed, because a container with
    a 60-second shutdown hook is not misbehaving and must not be reported as
    timed out at 30.

    ``is None``, not truthiness. ``timeout=0`` is a real and meaningful
    request -- it is ``docker stop -t 0``, "do not wait, kill it" -- and
    treating that zero as "unset" would silently grant a ten second grace to
    the one operator who explicitly asked for none.
    """
    if request.kind in (CommandKind.STOP, CommandKind.RESTART):
        grace = ENGINE_DEFAULT_GRACE if request.timeout is None else request.timeout
        return grace + DISPATCH_MARGIN
    return DEFAULT_DEADLINE


def _summarize(outcomes: tuple[TargetOutcome, ...]) -> str | None:
    """One line naming what went wrong, for the audit entry.

    Only failures. A successful command's detail is the outcomes themselves,
    and duplicating them into prose would double the log for nothing.
    """
    failures = [o for o in outcomes if not o.ok]
    if not failures:
        return None
    return "; ".join(f"{o.target.segments[-1][:12]}: {o.detail or o.status}" for o in failures)
