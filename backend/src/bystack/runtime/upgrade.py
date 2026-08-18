"""A fleet upgrade, one host at a time (ADR-0017).

**One click is a staged rollout, not a broadcast.** The Controller upgrades one
host, waits for its `Hello` to report the new version, and only then continues.
A failure stops the run rather than completing it.

Simultaneity is the whole of the risk. Applying a change to two hundred
machines at once is worth having only in proportion to the confidence that the
change is good, and the first host to run a release is the cheapest possible
place to discover it is not. What this buys is bounded by what it costs: a
fleet of forty takes a few minutes to roll, which is time nobody is watching
and every one of those minutes is a chance to stop.

It is deliberately not a scheduler. There is no queue that survives a restart,
no retry policy and no partial resumption, because a rollout is a thing an
operator starts while looking at the screen: a Controller that restarted
mid-run has forgotten, and the honest answer is a fleet whose version skew is
visible on the same panel that started it. Restarting the run skips the hosts
that already moved, because planning reads the versions rather than a record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from bystack.core.ports.provider import Provider
from bystack.infra.releases import Release, ReleaseStore
from bystack.providers.agent.provider import AgentProvider

log = logging.getLogger(__name__)

#: How long to wait for an upgraded host to come back and say what it is now.
#:
#: The agent is restarted by a process it does not control, dials out on its
#: own backoff and re-handshakes; seconds, on any host that works. This is the
#: deadline for the ones that do not, and it is deliberately far shorter than
#: the host's own 10-minute probation: the rollout's job is to stop, not to
#: watch a rollback happen. When it expires the host is still covered -- it
#: will restore its previous binary by itself -- and the run stops with the
#: sentence that says so.
CONFIRM_TIMEOUT = 120.0

#: How often to look while waiting for that.
CONFIRM_POLL = 1.0


class UpgradeUnavailable(Exception):
    """A rollout that cannot be started, with a reason worth showing."""


@dataclass(frozen=True, slots=True)
class HostResult:
    """What happened to one host, in the agent's own vocabulary.

    `state` is `confirmed`, `refused`, `failed` or `skipped`, and the four are
    kept apart because each sends the operator somewhere different: confirmed
    is done, refused is about the *release*, failed is about the *host*, and
    skipped is neither -- it is a host that was not in a position to
    participate, which on a real fleet is most of the offline ones.
    """

    engine_id: str
    state: str
    reason: str | None = None
    version: str = ""


@dataclass
class Rollout:
    """One run, as it happens and after it has finished."""

    version: str
    planned: tuple[str, ...]
    state: str = "running"
    """`running`, `finished`, `stopped` or `failed`.

    `stopped` is a run an operator ended and `failed` is one a host ended. The
    difference matters on the panel: the second names a host and a reason, and
    the first is not a fault.
    """

    current: str | None = None
    detail: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    results: list[HostResult] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for result in self.results if result.state == "confirmed")

    @property
    def running(self) -> bool:
        return self.state == "running"


class UpgradeService:
    """Starts, reports and stops the one rollout a Controller runs at a time.

    One at a time on purpose. Two concurrent rollouts would be two staged
    sequences interleaving on the same fleet, and the property that makes a
    staged rollout worth having -- that the previous host confirmed before the
    next one is touched -- is not something two runs can jointly guarantee.
    """

    __slots__ = ("_releases", "_providers", "_read_only", "_rollout", "_task", "_cancelled")

    def __init__(
        self,
        releases: ReleaseStore,
        providers: Callable[[], Mapping[str, Provider]],
        *,
        read_only: bool = False,
    ) -> None:
        self._releases = releases
        #: A closure rather than the collector, for the reason `CommandService`
        #: takes one: this needs to enumerate providers and nothing else, and
        #: holding the supervisor would let it grow a dependency on provider
        #: lifecycle it has no business having.
        self._providers = providers
        self._read_only = read_only
        self._rollout: Rollout | None = None
        self._task: asyncio.Task[None] | None = None
        self._cancelled = False

    @property
    def releases(self) -> ReleaseStore:
        return self._releases

    @property
    def rollout(self) -> Rollout | None:
        """The current or most recent run. `None` until one is started."""
        return self._rollout

    # -- planning ----------------------------------------------------------

    def candidates(self) -> list[AgentProvider]:
        """Hosts a release could be pushed to at all.

        Connected, not the Controller's own local agent, and running a build
        that verifies signed releases. Everything else in the fleet is
        upgraded the way it always was -- by running the installer on it once
        -- and the panel says so per host rather than leaving them out of the
        count with no explanation.
        """
        return [
            provider
            for provider in self._providers().values()
            if isinstance(provider, AgentProvider) and provider.upgradable
        ]

    def plan(self, version: str) -> list[AgentProvider]:
        """Who this run would touch, in the order it would touch them.

        Excludes hosts already on the target version, which is what makes
        restarting an interrupted run cheap: the answer comes from what each
        agent reported at its last `Hello`, not from a record of what this
        Controller did.

        Ordered by engine id. Not by "least important first", which is the
        thing an operator would ask for and which this cannot know -- there is
        no such field, inventing one would be configuration about topology
        (which ADR-0001 refuses), and a stable order at least means the first
        host is the same one on a second attempt.
        """
        return sorted(
            (
                provider
                for provider in self.candidates()
                if provider.agent_version != version
                and self._releases.find(version, provider.architecture) is not None
            ),
            key=lambda provider: provider.id,
        )

    # -- running -----------------------------------------------------------

    async def start(self, version: str | None = None) -> Rollout:
        """Begin a staged rollout. Returns as soon as it is under way.

        Deliberately not awaited to completion by its caller: a fleet of forty
        takes minutes, and an HTTP request that held one open would time out
        somewhere in the middle and leave the operator with no way to ask how
        far it got. The run reports itself through :attr:`rollout`.
        """
        if self._read_only:
            # A Controller in its safe mode does not replace executables on
            # other people's machines. Reads stay allowed -- `GET` on the
            # releases below is one -- because read-only is about mutation, and
            # this is the largest mutation in the product.
            raise UpgradeUnavailable(
                "this Controller is read-only, so it will not push a release to any host"
            )
        if self._rollout is not None and self._rollout.running:
            raise UpgradeUnavailable(
                f"a rollout of {self._rollout.version} is already running"
            )

        available = self._releases.versions()
        if not available:
            raise UpgradeUnavailable(
                f"there is no signed release in {self._releases.directory}. "
                f"Build one with scripts/build-agent.sh and sign it with "
                f"scripts/sign-agent.py."
            )
        version = version or available[0]
        if version not in available:
            raise UpgradeUnavailable(f"there is no signed release {version} here")

        planned = self.plan(version)
        if not planned:
            raise UpgradeUnavailable(
                f"no connected host needs {version}, or none that can verify a pushed "
                f"release. Hosts running an agent from before this feature are upgraded "
                f"by running install-agent.sh on them once."
            )

        self._cancelled = False
        rollout = Rollout(version=version, planned=tuple(p.id for p in planned))
        self._rollout = rollout
        self._task = asyncio.create_task(self._run(rollout))
        return rollout

    def cancel(self) -> Rollout | None:
        """Stop before the next host. Never mid-transfer.

        A host halfway through receiving an artifact is a host that has written
        a partial file and nothing else -- no trigger, no install -- so letting
        the current one finish is both harmless and the only way to leave the
        fleet in a state that describes itself.
        """
        self._cancelled = True
        return self._rollout

    async def aclose(self) -> None:
        """Stop a run that is under way, for shutdown."""
        self._cancelled = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self, rollout: Rollout) -> None:
        log.info(
            "rolling out agent %s to %d host(s)", rollout.version, len(rollout.planned)
        )
        try:
            for engine_id in rollout.planned:
                if self._cancelled:
                    _finish(rollout, "stopped", "stopped by an operator")
                    return
                rollout.current = engine_id
                if not await self._upgrade_one(rollout, engine_id):
                    return
            _finish(rollout, "finished", None)
        except asyncio.CancelledError:
            _finish(rollout, "stopped", "the Controller is shutting down")
            raise
        except Exception as exc:  # noqa: BLE001 - a run must not die silently
            log.exception("rollout of %s failed", rollout.version)
            _finish(rollout, "failed", f"the rollout itself failed: {exc}")
        finally:
            rollout.current = None

    async def _upgrade_one(self, rollout: Rollout, engine_id: str) -> bool:
        """One host. Returns whether the run should continue."""
        provider = self._providers().get(engine_id)
        if not isinstance(provider, AgentProvider) or not provider.upgradable:
            # Went away between planning and now, which on a real fleet is
            # ordinary and says nothing about the release. Recorded and stepped
            # over rather than treated as a failure -- stopping a rollout
            # because a laptop closed would make the feature unusable on
            # exactly the fleets it is for.
            rollout.results.append(
                HostResult(engine_id, "skipped", "it is no longer connected")
            )
            return True

        release = self._releases.find(rollout.version, provider.architecture)
        if release is None:
            rollout.results.append(
                HostResult(
                    engine_id,
                    "skipped",
                    f"there is no {rollout.version} build for "
                    f"{provider.architecture or 'this architecture'}",
                )
            )
            return True

        outcome = await provider.push_upgrade(release)
        if not outcome.staged:
            # A refusal is about the release and every other host will refuse
            # it identically; a failure is about this host. Both stop the run,
            # because the alternative is discovering the first case forty times.
            rollout.results.append(HostResult(engine_id, outcome.state, outcome.reason))
            _finish(
                rollout,
                "failed",
                f"{engine_id}: {outcome.reason or outcome.state}",
            )
            return False

        confirmed, detail = await self._confirm(engine_id, rollout.version)
        rollout.results.append(
            HostResult(
                engine_id,
                "confirmed" if confirmed else "failed",
                detail,
                rollout.version if confirmed else "",
            )
        )
        if not confirmed:
            _finish(rollout, "failed", f"{engine_id}: {detail}")
            return False
        return True

    async def _confirm(self, engine_id: str, version: str) -> tuple[bool, str | None]:
        """Wait for the host to come back saying it is the new version.

        **The `Hello` is the test, not the install.** A binary that starts is
        not an agent that works: a protocol regression or a panic on an
        architecture the release was not tested on produces a process systemd
        reports as running and a host that is on nobody's map. So what is
        waited for is the same event the host's own probation is waiting for --
        an admitted connection carrying the new version.

        Deliberately does not wait out the host's rollback. If it does not come
        back, the host restores its previous binary by itself within ten
        minutes, and the rollout's job at that point is to stop and say which
        host it stopped on.
        """
        deadline = time.monotonic() + CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            provider = self._providers().get(engine_id)
            if isinstance(provider, AgentProvider) and provider.agent_version == version:
                return True, None
            await asyncio.sleep(CONFIRM_POLL)
        return False, (
            f"it staged {version} but has not come back on it within "
            f"{CONFIRM_TIMEOUT:.0f}s. It will restore its previous agent by itself; "
            f"nothing else has been upgraded."
        )


def _finish(rollout: Rollout, state: str, detail: str | None) -> None:
    rollout.state = state
    rollout.detail = detail
    rollout.finished_at = time.time()
    rollout.current = None
    log.info("rollout of %s %s%s", rollout.version, state, f": {detail}" if detail else "")


def skew(providers: Iterable[Provider], version: str | None) -> list[str]:
    """Hosts not running ``version``. The report ADR-0015 built, reused.

    A rollout needs no progress model of its own: the version per host is
    already on `GET /agents` and already drawn on the panel that starts one.
    This is the same question asked of the provider set, for the one caller
    that wants a count rather than a table.
    """
    if not version:
        return []
    return [
        provider.id
        for provider in providers
        if isinstance(provider, AgentProvider)
        and provider.agent_version
        and provider.agent_version != version
    ]


__all__ = [
    "CONFIRM_TIMEOUT",
    "HostResult",
    "Release",
    "Rollout",
    "UpgradeService",
    "UpgradeUnavailable",
    "skew",
]
