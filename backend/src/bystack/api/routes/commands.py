"""Operations API.

The mutating surface. Three endpoints, and the asymmetry between them is
intentional: two cheap reads that the UI may call freely, and one write that
goes through :class:`~bystack.runtime.commands.CommandService` and nowhere
else.

This module contains no policy. It parses, delegates, and maps a refusal onto
a status code. Every decision about whether an operation is permitted lives
in the service, so that a second entry point cannot be written that forgets
one.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Query

from bystack.api.deps import Commands, Context
from bystack.api.schemas import (
    ActionsOut,
    AuditEntryOut,
    CommandIn,
    CommandResultOut,
    GroupCommandHostOut,
    GroupCommandIn,
    GroupCommandOut,
)
from bystack.core.identity import URN, URNError, engine_scope, process_urn, unit_urn
from bystack.core.ports.command import (
    CommandRejected,
    CommandRequest,
    CommandStatus,
    RejectionReason,
)
from bystack.core.ports.watch import WatchEntry, WatchKind

router = APIRouter(prefix="/commands", tags=["operations"])

#: Refusal to status code.
#:
#: ``403`` for read-only rather than ``405``: the endpoint exists and the verb
#: is right, the caller is simply not permitted. ``409`` for an unsupported
#: target or state, because both mean "not in a condition where this makes
#: sense" and both can become true later without the client changing anything
#: -- which is exactly what conflict semantics are for.
_STATUS: Final[dict[RejectionReason, int]] = {
    RejectionReason.READ_ONLY: 403,
    RejectionReason.UNKNOWN_TARGET: 404,
    RejectionReason.UNSUPPORTED_TARGET: 409,
    RejectionReason.UNSUPPORTED_STATE: 409,
    RejectionReason.TOO_MANY_TARGETS: 422,
    RejectionReason.PROVIDER_UNAVAILABLE: 503,
}


@router.post("", response_model=CommandResultOut, summary="Run an operation")
async def run_command(body: CommandIn, service: Commands) -> CommandResultOut:
    """Execute an operation against a container, service or stack.

    Synchronous by design. These operations take seconds, the operator is
    watching, and an accepted-then-poll protocol would add a job store, an
    id-to-status endpoint and a whole class of orphaned-job bugs to buy
    nothing. The command that genuinely needs asynchrony -- a scheduled or
    fleet-wide rollout -- is a different feature with a different shape.

    **The graph is not updated by this call.** The response says what the
    engine did; the topology changes when discovery observes it, moments
    later, over the delta stream. A client that patches its own graph from
    this response is reintroducing exactly the drift the design removes.
    """
    request = CommandRequest(
        kind=body.kind,
        target=_parse_urn(body.target),
        timeout=body.timeout,
        signal=body.signal,
        reason=body.reason,
    )

    try:
        result = await service.execute(request)
    except CommandRejected as exc:
        raise HTTPException(
            status_code=_STATUS.get(exc.reason, 400),
            detail={"reason": str(exc.reason), "message": exc.detail},
        ) from exc

    return CommandResultOut.of(result)


@router.post("/group", response_model=GroupCommandOut, summary="Run an operation across hosts")
async def run_group_command(
    body: GroupCommandIn, service: Commands, context: Context
) -> GroupCommandOut:
    """Run one lifecycle command on one watch group, across the hosts named.

    **N ordinary commands, and nothing new underneath.** Each host resolves to
    one node and goes through :class:`~bystack.runtime.commands.CommandService`
    exactly as a click on that card would — same read-only choke point, same
    expansion, same audit. This route resolves and aggregates; it decides
    nothing, which is why it can exist without reopening ADR-0014.

    **One audit entry per host, deliberately.** These are N operations on N
    machines that happened to be asked for together, and a single record
    claiming "restarted the group" would hide which machine actually took it —
    the only thing worth knowing afterwards. The shared `group_id` is how they
    are tied back together.

    **One host's refusal is not the request's.** A machine that is asleep, or
    that holds no entry from this group, is reported in its own row
    while the rest proceed. The exception is read-only, which is a fact about
    this Controller rather than about any host: when it is the *only* thing
    that happened, the whole request is a `403` and not eight identical rows.
    """
    targets = list(dict.fromkeys(engine_scope(engine_id) for engine_id in body.engine_ids))
    hosts: list[GroupCommandHostOut] = []

    for engine_id in targets:
        entry = next(
            (
                candidate
                for candidate in context.watchlist.entries(engine_id)
                if candidate.group_id == body.group_id
            ),
            None,
        )
        if entry is None:
            # Removed on that host since the operator's screen was drawn, never
            # there, or -- the one that is easy to miss -- from a selection that
            # stored nothing anywhere: the fan-out mints a group id per request
            # and a host that already watches the target refuses the duplicate,
            # so a group can be born with no members at all.
            #
            # Which is why this does not say "no longer". Only the first of the
            # three is a host that ever held the entry, and a message asserting
            # it sends an operator looking for the moment it was removed.
            #
            # Named rather than skipped: a host that silently disappears from
            # the answer is indistinguishable from one that was never asked.
            hosts.append(
                GroupCommandHostOut(
                    engine_id=engine_id,
                    ran=False,
                    status=str(RejectionReason.UNKNOWN_TARGET),
                    detail="this host watches nothing from this selection",
                )
            )
            continue

        urn = _urn_of(entry)
        try:
            result = await service.execute(
                CommandRequest(
                    kind=body.kind,
                    target=urn,
                    timeout=body.timeout,
                    signal=body.signal,
                    reason=body.reason,
                )
            )
        except CommandRejected as exc:
            hosts.append(
                GroupCommandHostOut(
                    engine_id=engine_id,
                    urn=str(urn),
                    ran=False,
                    status=str(exc.reason),
                    detail=exc.detail,
                )
            )
            continue

        hosts.append(
            GroupCommandHostOut(
                engine_id=engine_id,
                urn=str(urn),
                ran=True,
                status=str(result.status),
                result=CommandResultOut.of(result),
            )
        )

    if hosts and all(host.status == str(RejectionReason.READ_ONLY) for host in hosts):
        # Not a partial anything. The control plane is read-only and no host
        # was ever going to take this, so it gets the same answer a single
        # command does rather than a body full of identical rows.
        raise HTTPException(
            status_code=403,
            detail={
                "reason": str(RejectionReason.READ_ONLY),
                "message": hosts[0].detail or "the control plane is running read-only",
            },
        )

    return GroupCommandOut(
        kind=str(body.kind),
        group_id=body.group_id,
        status=_worst(hosts),
        hosts=hosts,
    )


def _urn_of(entry: WatchEntry) -> URN:
    """The node a watch entry becomes.

    The same rule `WatchEntryOut.urn` applies, and it lives in both places for
    the reason the frontend does not have a third copy: a unit is named by
    systemd and a process has no name at all, so its identity is the *rule*
    the operator wrote.
    """
    if entry.kind is WatchKind.UNIT:
        return unit_urn(entry.engine_id, entry.name)
    return process_urn(entry.engine_id, entry.id)


#: Worst-wins, most severe first.
#:
#: Deliberately the same order `CommandResult.status` uses across the targets
#: *within* one host, applied again across hosts. A second ordering here would
#: mean a group of one could report differently from the same command sent to
#: that host on its own.
_SEVERITY: Final[tuple[CommandStatus, ...]] = (
    CommandStatus.FAILED,
    CommandStatus.REJECTED,
    CommandStatus.TIMED_OUT,
)


def _worst(hosts: list[GroupCommandHostOut]) -> str:
    """One answer for the whole scope, erring towards "go and look".

    A group restart where eight machines succeeded and the ninth is asleep is
    a failure. Reporting it as success because the majority worked is how an
    operator walks away from a half-restarted fleet -- the same sentence the
    per-host rule is written from, and the reason it is repeated rather than
    softened at this level.
    """
    if not hosts:
        return str(CommandStatus.NOOP)
    # A host that never ran is a failure of the whole, whatever the reason:
    # the operator asked for N machines and reached fewer.
    if any(not host.ran for host in hosts):
        return str(CommandStatus.FAILED)
    seen = {host.status for host in hosts}
    for status in _SEVERITY:
        if str(status) in seen:
            return str(status)
    if seen == {str(CommandStatus.NOOP)}:
        return str(CommandStatus.NOOP)
    return str(CommandStatus.SUCCEEDED)


@router.get("/actions", response_model=ActionsOut, summary="Available operations")
async def get_actions(
    service: Commands, urn: str = Query(description="Target URN")
) -> ActionsOut:
    """What may be done to a node right now, given its state and ours.

    The UI renders buttons from this rather than deciding for itself, so
    "unpause applies to paused containers" is written down once. A second
    copy in the frontend would be correct the day it was written and wrong by
    the release that adds a state.

    Never 404s for an unknown node: it returns a reason instead, because this
    is a UI affordance query and a selected node vanishing mid-deploy is
    normal, not exceptional.
    """
    return ActionsOut.of(service.actions(_parse_urn(urn)))


@router.get("/audit", response_model=list[AuditEntryOut], summary="Recent operations")
async def get_audit(
    service: Commands,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[AuditEntryOut]:
    """Every operation this process attempted, newest first.

    Includes refusals. A read-only control plane that declined to restart a
    dead service is the single most useful line this log can hold, and a log
    that recorded only what succeeded would omit it.

    Durable by default since the audit log moved to disk
    (`infra/audit/durable.py`), so this answers about previous runs of the
    Controller and not only this one. What it deliberately does not answer is
    *who*: ADR-0014 decides this platform has no user identity, so every entry
    reads `anonymous` -- and that is also why no destructive operation exists
    or will. The verbs it records are all reversible, which is what makes an
    unattributed log a complete record rather than half of one.
    """
    return [AuditEntryOut.of(entry) for entry in service.audit.recent(limit)]


def _parse_urn(raw: str) -> URN:
    try:
        return URN(raw)
    except URNError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
