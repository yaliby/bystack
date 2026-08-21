"""Updating the Controller this dashboard is served by (ADR-0018).

Three routes, and they are the whole operator surface: what this machine is,
ask it to update itself, how far it has got.

There is **no progress model here** and no state held between the calls, for
the reason ADR-0017's rollout routes hold none: everything an operator sees is
what `bystack-manager` wrote to a file as root. That is not a limitation being
worked around -- it is the only design that survives the middle of the
operation, because the middle of the operation is *this process being stopped
and replaced*. A progress model in memory would be lost at exactly the moment
somebody is watching it.

Served on the browser-facing port, like enrollment and the fleet rollout, and
for the same reason: this is a decision about what runs, and it belongs on the
side an operator reaches rather than the side agents dial. Nothing an agent
sends can reach any of it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bystack.api.deps import Context
from bystack.infra.manager import ManagerError, UpdateStatus

router = APIRouter(prefix="/controller", tags=["system"])


class UpdateStatusOut(BaseModel):
    """What the local updater last reported."""

    phase: str
    """`fetching`, `verifying`, `applying`, `probation`, `cascading`,
    `success`, `failed` or `rolled_back`.

    A closed set rather than a percentage. Each one is a different sentence
    under a progress bar, and `rolled_back` in particular is not a failure to
    report as one: the machine is working, on the version it was.
    """

    version: str
    detail: str
    cascade: str = ""
    updated_at: int = 0
    running: bool = False

    @classmethod
    def of(cls, status: UpdateStatus) -> UpdateStatusOut:
        return cls(
            phase=status.phase,
            version=status.version,
            detail=status.detail,
            cascade=status.cascade,
            updated_at=status.updated_at,
            running=status.running,
        )


class ControllerOut(BaseModel):
    """This machine, and whether it can update itself.

    `updatable` is false far more often than not -- a container, a checkout, a
    `pip install` into a venv -- so `reason` is here to say which, in a
    sentence. An install path that silently has no button looks broken; one
    that says "this is a container, upgrade the image" is documentation in the
    place the question is asked.
    """

    version: str
    updatable: bool
    reason: str = ""
    read_only: bool = False
    update: UpdateStatusOut | None = None


class UpdateIn(BaseModel):
    version: str = Field(default="", description="Which release. Empty means the newest.")


@router.get("", response_model=ControllerOut, summary="This Controller, and its updater")
async def controller(context: Context) -> ControllerOut:
    """A read, answered whatever `read_only` is set to.

    Refusing to *show* an operator what version they are running because the
    platform is in its safe mode would be the same mistake as refusing to show
    them a container's log.
    """
    from bystack import __version__

    service = context.selfupdate
    status = service.status()
    return ControllerOut(
        version=__version__,
        updatable=service.available,
        reason=(
            ""
            if service.available
            else (
                f"there is no local updater at {service.link.directory}. That is what a "
                f"container, a checkout or a `pip install` looks like -- upgrade this "
                f"Controller the way it was installed."
            )
        ),
        read_only=context.settings.read_only,
        update=UpdateStatusOut.of(status) if status is not None else None,
    )


@router.post("/update", response_model=UpdateStatusOut | None, summary="Update this Controller")
async def update(body: UpdateIn, context: Context) -> UpdateStatusOut | None:
    """Write the intent and return immediately.

    Returns whatever the status file says *now*, which is normally the previous
    run or `null`: root has not been started yet, and inventing a `queued`
    state here would be this process making a claim about a file it has not
    read. The dashboard polls `GET /controller` from the next tick, and the
    first thing it sees is the manager's own first line.

    409 for everything that makes an update impossible -- read-only, no local
    updater, a run already going. Each is a state rather than a bad request,
    and each carries the sentence that says what to do about it.
    """
    try:
        context.selfupdate.request(body.version)
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = context.selfupdate.status()
    return UpdateStatusOut.of(status) if status is not None else None


@router.get("/update", response_model=UpdateStatusOut | None, summary="How far the update got")
async def status(context: Context) -> UpdateStatusOut | None:
    """`null` until this machine has ever run one. Polled while one is going.

    Its own route as well as a field on `GET /controller`, because during a run
    this is polled every two seconds and the rest of that answer does not move.
    """
    current = context.selfupdate.status()
    return UpdateStatusOut.of(current) if current is not None else None
