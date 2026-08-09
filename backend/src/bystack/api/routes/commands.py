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

from bystack.api.deps import Commands
from bystack.api.schemas import ActionsOut, AuditEntryOut, CommandIn, CommandResultOut
from bystack.core.identity import URN, URNError
from bystack.core.ports.command import CommandRejected, CommandRequest, RejectionReason

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
