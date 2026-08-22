"""Updating the Controller itself, and cascading to the fleet (ADR-0018).

Two phases, and the second one is not a new mechanism.

**Phase one** is the Controller's own. The dashboard's button lands here, this
writes `update.intent`, and a path unit starts `bystack-manager` as root. From
that moment this process is a *reporter*: it polls the status file and serves
what root wrote, right up until root stops it and starts its replacement. The
progress bar the operator is watching is being served by the process being
replaced, which is why the status file has to outlive it -- and does, because
it is a file.

**Phase two** is the fleet's, and it is ADR-0017's staged rollout unchanged.
When the manager has a healthy new Controller it drops the matching signed
agent artifacts into the release directory and names the version in
`cascade`. The new Controller comes up, sees a cascade for *its own version*
that it has not run, and starts the rollout it already knows how to run.

The condition is narrow on purpose:

* `cascade` must equal this Controller's own version. A cascade naming anything
  else is a report about a machine in a different state -- an update that rolled
  back, or a status file left from last time -- and starting a fleet-wide
  rollout on the strength of it would be acting on a stale sentence.
* it must not already have been run. Recorded here, in the Controller's state
  directory, because the status file is root's and this process cannot write to
  it. A restart is otherwise a second rollout.

**"Not yet" is not "no".** A rollout is planned from the hosts connected at that
instant, and this process was started by the updater a moment ago -- so the
first look after a self-update finds an empty fleet that is still dialling back
in. That one case is retried for the length of the window rather than recorded
as done, which is the difference between a fleet that rolls forward by itself
and one that silently does not.

**Dropping artifacts into the release directory does not, by itself, cascade.**
An operator who copies files in by hand gets what they always got: a release
the dashboard offers and a button to roll it out. Only an update this machine
performed starts one without being asked, because only then is there evidence
about what just changed and why.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from bystack.infra.manager import ManagerError, ManagerLink, UpdateStatus
from bystack.runtime.upgrade import UpgradeUnavailable

log = logging.getLogger(__name__)

#: How often to look at the status file.
#:
#: Two seconds while a run is in flight, because that is what a progress bar
#: reads as live. Nothing at all otherwise: the watcher below only exists for
#: the cascade, and the cascade is decided once, at startup, from a file that
#: root wrote before this process was started.
POLL_SECONDS = 2.0

#: How long after startup to keep looking for a cascade.
#:
#: The manager writes `cascade` *before* starting the new Controller in the
#: ordinary case, so it is normally there on the first read. This window covers
#: the case where phase two is still downloading the fleet's artifacts while
#: the Controller it just installed is already serving -- which is the design,
#: because the Controller is usable during it.
CASCADE_WINDOW_SECONDS = 600.0

#: The file that says a cascade has been acted on. One line, the version.
MARKER = "cascaded"


class SelfUpdateService:
    """What the dashboard's "update this Controller" button reaches.

    Holds no state about a run. Everything an operator sees comes from the
    status file root writes, because the alternative is a second model of the
    same update that disagrees with it exactly when something goes wrong -- and
    which would in any case be lost when this process is stopped half-way
    through, which is a *normal* step of the thing being modelled.
    """

    __slots__ = ("_link", "_version", "_health", "_state_dir", "_read_only", "_task")

    def __init__(
        self,
        link: ManagerLink,
        *,
        version: str,
        health: str,
        state_dir: str | Path,
        read_only: bool = False,
    ) -> None:
        self._link = link
        self._version = version
        #: Always loopback, never the configured bind address. A Controller on
        #: `0.0.0.0` still answers on `127.0.0.1`, and the manager refuses a
        #: health URL that is not on this machine -- a probation check that
        #: could be pointed elsewhere is one that always passes.
        self._health = health
        self._state_dir = Path(state_dir).expanduser()
        self._read_only = read_only
        self._task: asyncio.Task[None] | None = None

    @property
    def link(self) -> ManagerLink:
        return self._link

    @property
    def available(self) -> bool:
        return self._link.installed

    def status(self) -> UpdateStatus | None:
        return self._link.status()

    def request(self, version: str = "") -> None:
        """Ask the manager for an update. Raises with a sentence, or returns.

        `read_only` is honoured here even though this changes nothing about
        any managed host. It is the strongest thing this Controller can be
        asked to do to itself, and a deployment whose operator turned the
        platform's safe mode on has said they want to watch rather than touch.
        """
        if self._read_only:
            raise ManagerError(
                "this Controller is read-only. Updating itself is the largest change it "
                "can make, so it is one of the things that mode is for."
            )
        current = self._link.status()
        if current is not None and current.running:
            raise ManagerError(
                f"an update is already {current.phase}. One at a time: two managers "
                f"renaming the same file is not a state anyone could describe."
            )
        self._link.request(version, self._health)

    # -- the cascade -------------------------------------------------------

    def start(self, rollout: Callable[[str], Awaitable[None]]) -> None:
        """Watch for a cascade this Controller should act on.

        Given the rollout rather than the `UpgradeService`, for the reason
        that service takes a closure over the providers: this needs to be able
        to start one run and nothing else, and holding the service would let it
        grow opinions about rollouts it has no business having.
        """
        if self._task is None:
            self._task = asyncio.create_task(self._watch(rollout), name="bystack-cascade")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _watch(self, rollout: Callable[[str], Awaitable[None]]) -> None:
        deadline = time.monotonic() + CASCADE_WINDOW_SECONDS
        while time.monotonic() < deadline:
            status = self._link.status()
            if status is not None and self._should_cascade(status):
                log.info(
                    "the local updater installed %s and left signed agents for it; "
                    "rolling the fleet forward",
                    status.cascade,
                )
                try:
                    await rollout(status.cascade)
                except UpgradeUnavailable as exc:
                    # **Not a failure, and not yet.** This process was started
                    # by the updater seconds ago; the fleet is still dialling
                    # back in, and a rollout plan is built from hosts that are
                    # *connected right now*. So the first look after a
                    # self-update reliably finds nobody, which is the one case
                    # that has to be retried rather than recorded.
                    #
                    # Nothing is written here, so the next tick tries again --
                    # for as long as the window lasts, and no longer. A fleet
                    # that never comes back is a fleet with a bigger problem
                    # than a missed rollout, and it is visible on the Hosts
                    # panel as version skew with a button beside it.
                    log.debug("cascade not startable yet: %s", exc)
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                except Exception:  # noqa: BLE001 - a rollout that will not start is a log line
                    # Anything else is a real refusal and will refuse again.
                    # Recorded so it is not retried on every restart.
                    self._record(status.cascade)
                    log.exception("could not start the cascading rollout")
                    return
                # Recorded once the run has actually *begun*. `start` creates
                # the task and returns, so this is still before the rollout
                # finishes -- which is what stops a Controller that crashed
                # mid-rollout from starting a second one on every boot.
                self._record(status.cascade)
                return
            await asyncio.sleep(POLL_SECONDS)

    def _should_cascade(self, status: UpdateStatus) -> bool:
        if status.phase != "success" or not status.cascade:
            return False
        if status.cascade != self._version:
            # A cascade for a version this is not means the file describes a
            # different machine-state than the one running: a rolled-back
            # update, or a status left from before. Neither is a reason to
            # touch a fleet.
            return False
        return self._recorded() != status.cascade

    def _marker(self) -> Path:
        return self._state_dir / MARKER

    def _recorded(self) -> str:
        try:
            return self._marker().read_text().strip()
        except OSError:
            return ""

    def _record(self, version: str) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._marker().write_text(f"{version}\n")
        except OSError as exc:
            # Not fatal, and worth a sentence: the cost is a second rollout on
            # the next restart, which is idempotent -- planning reads the
            # versions rather than a record, so hosts that already moved are
            # skipped (`runtime/upgrade.py`).
            log.warning("cannot record the cascade: %s", exc)
