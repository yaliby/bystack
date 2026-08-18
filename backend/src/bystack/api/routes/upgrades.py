"""Distributing a signed agent release to the fleet (ADR-0017).

Four routes, and between them they are the whole operator surface: what this
Controller can hand out, who would take it, start the run, stop the run. There
is no per-host route and no "upgrade this one" button, and that is the design
rather than an omission — one click is a **staged rollout**, because
simultaneity is the entire risk of the feature and a per-host endpoint is a
broadcast with extra steps.

There is also no progress model here beyond which host is being touched right
now. The version per host is already on `GET /agents` and already drawn on the
panel that starts a rollout (ADR-0015's skew report), so a run's progress is
the fleet's own state changing rather than a second thing to keep in step with
it.

Served on the **browser-facing** port, like enrollment and for the same reason:
these are the decisions about what runs on the fleet, and they belong on the
side an operator reaches rather than the side agents dial. Nothing an agent
sends can reach any of this.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bystack.api.deps import Context
from bystack.runtime.upgrade import HostResult, Rollout, UpgradeUnavailable

router = APIRouter(prefix="/agents", tags=["agents"])


class ReleaseOut(BaseModel):
    """One artifact this Controller could distribute."""

    version: str
    arch: str
    sha256: str
    released_at: int
    size: int


class ReleasesOut(BaseModel):
    """What is on hand, and who would take it.

    `directory` is included because the answer is empty far more often than it
    is not, and an empty list with no path is a feature that looks broken. It
    names the place to put a file.
    """

    directory: str
    releases: list[ReleaseOut]
    upgradable: list[str]
    """Hosts that could be sent one right now: connected, not this machine's
    own agent, and running a build that verifies signatures."""

    stale: list[str]
    """Connected hosts that **cannot** take a pushed release.

    Named rather than left out of `upgradable` silently. An agent from before
    this feature is upgraded by running the installer on it once, and an
    operator who cannot see which hosts those are has no way to know why the
    rollout they just ran left four machines behind.
    """


class RolloutIn(BaseModel):
    version: str = Field(default="", description="Which release. Empty means the newest.")


class HostResultOut(BaseModel):
    engine_id: str
    state: str
    reason: str | None = None
    version: str = ""

    @classmethod
    def of(cls, result: HostResult) -> HostResultOut:
        return cls(
            engine_id=result.engine_id,
            state=result.state,
            reason=result.reason,
            version=result.version,
        )


class RolloutOut(BaseModel):
    version: str
    state: str
    planned: list[str]
    current: str | None = None
    detail: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    results: list[HostResultOut] = Field(default_factory=list)

    @classmethod
    def of(cls, rollout: Rollout) -> RolloutOut:
        return cls(
            version=rollout.version,
            state=rollout.state,
            planned=list(rollout.planned),
            current=rollout.current,
            detail=rollout.detail,
            started_at=rollout.started_at,
            finished_at=rollout.finished_at,
            results=[HostResultOut.of(result) for result in rollout.results],
        )


@router.get("/releases", response_model=ReleasesOut, summary="Releases this Controller holds")
async def releases(context: Context) -> ReleasesOut:
    """What could be pushed, and to whom.

    A read, and one an operator performs before deciding anything, so it is
    answered whatever the Controller's `read_only` setting is. Refusing to
    *show* someone what is on the disk because the platform is in its safe mode
    would be the same mistake as refusing to show them a container's log.
    """
    service = context.upgrades
    candidates = {provider.id for provider in service.candidates()}
    connected = {
        provider.id
        for provider in context.collector.providers.values()
        if getattr(provider, "connected", False)
    }
    return ReleasesOut(
        directory=str(service.releases.directory),
        releases=[
            ReleaseOut(
                version=release.version,
                arch=release.arch,
                sha256=release.sha256,
                released_at=release.released_at,
                size=release.size,
            )
            for release in service.releases.all()
        ],
        upgradable=sorted(candidates),
        stale=sorted(connected - candidates),
    )


@router.get("/upgrades", response_model=RolloutOut | None, summary="The current or last rollout")
async def current(context: Context) -> RolloutOut | None:
    """`null` until one has been started. Polled while one is running."""
    rollout = context.upgrades.rollout
    return RolloutOut.of(rollout) if rollout is not None else None


@router.post("/upgrades", response_model=RolloutOut, summary="Roll a release out to the fleet")
async def start(body: RolloutIn, context: Context) -> RolloutOut:
    """Begin a staged rollout: one host, confirmed, then the next.

    Returns as soon as the run is under way rather than when it finishes. A
    fleet of forty takes minutes, and a request held open for that would time
    out somewhere in the middle and leave the operator unable to ask how far it
    got — so progress is `GET /agents/upgrades` and the version chips on the
    Hosts panel, which were already there.

    409 for everything that makes a rollout impossible: read-only, a run
    already going, nothing signed to send, nobody to send it to. Each of those
    is a state rather than a bad request, and each carries the sentence that
    says what to do about it.
    """
    try:
        return RolloutOut.of(await context.upgrades.start(body.version or None))
    except UpgradeUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/upgrades/cancel", response_model=RolloutOut, summary="Stop after the current host")
async def cancel(context: Context) -> RolloutOut:
    """Stop the run. Never mid-transfer.

    A host halfway through receiving an artifact has written a partial file and
    nothing else — no trigger, no install — so the current host is allowed to
    finish. Cancelling *during* a transfer would leave a fleet in a state
    nobody could describe, to save one machine's worth of seconds.
    """
    rollout = context.upgrades.cancel()
    if rollout is None:
        raise HTTPException(status_code=404, detail="no rollout has been started")
    return RolloutOut.of(rollout)
