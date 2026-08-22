"""Zero-config startup: the agent the Controller runs for its own machine.

`python -m bystack` with no configuration used to manage the local engine, and
that property is the whole first-run experience. Deleting the agentless path
took it away (`docs/MIGRATION.md` §4): the Controller reaches no Docker socket,
so a first run showed an empty graph and an instruction to go and install
something.

This gives it back **without giving discovery a second implementation**. The
Controller bundles the agent binary and spawns it locally, connected over a
unix socket instead of TLS. Same binary, same protobuf, same informer, same
ingest path, same partition writer — the only differences are the pipe
underneath the frames and the absence of enrollment, because a socket only this
user can open is the authentication (`runtime/trust.py`, ``admit_local``).

The rejected alternative was keeping the Python informer alive for the local
case. It would mean maintaining two complete implementations of discovery
forever, so that the easiest deployment could skip a subprocess. **One code
path is worth more than one process.**

Nothing here decides anything about the graph. It owns a socket, a child
process and a backoff, and it is the only module in the Controller that knows
an agent can be something other than a stranger that dialled in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from bystack.config import Settings

log = logging.getLogger(__name__)

#: Where a packaged Controller keeps the agent it ships with.
#:
#: Empty in a source checkout, which is why `_find_binary` falls through to the
#: build directory. Populating it is packaging's job (`docs/MIGRATION.md` §7)
#: and nothing here changes when it does.
BUNDLED = Path(__file__).resolve().parent.parent / "_bundled" / "bystack-agent"

#: A development checkout: `backend/src/bystack/runtime/` -> `agent/target/…`.
#:
#: Last in the search order and never in a shipped layout. It exists so that a
#: contributor who has run `cargo build --release` gets the same first-run
#: experience as someone who installed a package, without a config file.
IN_TREE = (
    Path(__file__).resolve().parents[4] / "agent" / "target" / "release" / "bystack-agent"
)

#: `scripts/install-agent.sh`, as carried in the wheel (`hatch_build.py`).
#:
#: Uploaded to a machine the Controller is deploying an agent to (ADR-0019), so
#: that the target needs no outbound internet of its own. It is text and it is
#: never executed here.
BUNDLED_INSTALLER = Path(__file__).resolve().parent.parent / "_bundled" / "install-agent.sh"

#: The same file in a development checkout, for the same reason `IN_TREE`
#: exists: a contributor running from source gets the working button rather
#: than a sentence about a wheel they did not build.
IN_TREE_INSTALLER = Path(__file__).resolve().parents[4] / "scripts" / "install-agent.sh"

#: Restart backoff. Same shape and the same reason as the agent's own
#: (`agent/src/main.rs`): a child that cannot start must not be respawned in a
#: hot loop, and a Docker daemon that is slow to come up is an ordinary cause.
BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0

#: How long a child must survive before its exit counts as a fresh failure
#: rather than a continuing one. Without it, an agent that runs happily for a
#: week and then loses its socket would be restarted on a minute-long backoff
#: inherited from a bad start a week earlier.
HEALTHY_RUN = 60.0

#: How long a terminated child gets to exit before it is killed.
STOP_GRACE = 5.0


class LocalAgentState(Enum):
    """What the local agent is doing, in the terms an operator would ask in."""

    DISABLED = "disabled"
    """Turned off in configuration. Not a fault, and never reported as one."""

    UNAVAILABLE = "unavailable"
    """Enabled, and something it needs is missing — no binary, no Docker
    socket. The reason is the whole value of this state: it is the difference
    between an empty graph that is a bug and an empty graph that is a
    machine with no containers on it."""

    STARTING = "starting"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class LocalAgentStatus:
    state: LocalAgentState
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in (LocalAgentState.STARTING, LocalAgentState.RUNNING)


class LocalAgent:
    """Owns the socket, the child process and the restarts.

    Constructed always and started conditionally: `status` is meaningful before
    `start` is called, which is what lets the API explain a first run where the
    agent could not be spawned at all rather than reporting an empty fleet with
    no reason attached.
    """

    __slots__ = (
        "_settings",
        "_binary",
        "_status",
        "_process",
        "_task",
        "_stopping",
        "socket_path",
        "engine_socket",
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._binary: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        #: Where the agent dials. Under the state directory by default, which
        #: is this user's and already exists for the CA key.
        self.socket_path = _resolve(
            settings.local_agent.socket,
            Path(settings.agents.state_dir).expanduser() / "local-agent.sock",
        )
        #: The engine the child reads. Resolved once here rather than at spawn
        #: time so that the answer cannot differ between the check and the use.
        self.engine_socket = Path(settings.local_agent.docker_socket).expanduser()

        self._status = self._assess()

    # -- what an operator can be told --------------------------------------

    @property
    def status(self) -> LocalAgentStatus:
        return self._status

    def _assess(self) -> LocalAgentStatus:
        """Decide whether this can run at all, before trying.

        Every failure here is a *configuration* answer rather than a runtime
        one, and each names the thing that is missing. The alternative — spawn
        and let it fail — reports "the agent exited with status 1" for three
        unrelated causes.
        """
        config = self._settings.local_agent
        if not config.enabled:
            return LocalAgentStatus(LocalAgentState.DISABLED, "local_agent.enabled is false")

        if not self.engine_socket.exists():
            return LocalAgentStatus(
                LocalAgentState.UNAVAILABLE,
                f"no Docker socket at {self.engine_socket}. "
                "This machine has no engine to manage; other hosts still join by enrolling.",
            )

        binary, reason = _find_binary(config.binary)
        if binary is None:
            return LocalAgentStatus(LocalAgentState.UNAVAILABLE, reason)
        self._binary = binary

        return LocalAgentStatus(LocalAgentState.STARTING, f"spawning {binary}")

    # -- lifecycle ---------------------------------------------------------

    def bind(self) -> socket.socket | None:
        """Create the listening socket, or ``None`` when there is nothing to run.

        Bound here rather than by uvicorn because the mode matters and uvicorn
        opens a unix socket world-writable. The umask is what makes it 0600 at
        creation: a `bind` then `chmod` leaves a window in which anything on
        the machine could have connected and claimed to be this host's agent.

        **The caller owns what comes back**, and closes it. Whoever serves a
        socket closes it; keeping a second reference here to close as well is
        how a shutdown ends in `Invalid file descriptor`. This object keeps the
        path, not the descriptor, and :meth:`stop` unlinks it.
        """
        if not self._status.ok:
            return None

        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        # A socket left behind by a Controller that was killed rather than
        # stopped. Removing it is safe precisely because nothing else may
        # write in this directory.
        path.unlink(missing_ok=True)

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous)
        listener.listen(8)
        return listener

    async def start(self) -> None:
        """Spawn the child and keep it alive until :meth:`stop`."""
        if not self._status.ok or self._binary is None:
            log.info("local agent not started: %s", self._status.detail)
            return
        self._task = asyncio.create_task(self._supervise(), name="local-agent")

    async def stop(self) -> None:
        """Terminate the child and release the socket.

        The order is deliberate: stop the process first, so it does not spend
        its backoff dialling a socket that is being unlinked underneath it.
        """
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        await self._terminate()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # The path, not the descriptor: whoever is serving the socket closes
        # it, and closing it twice is the shutdown that ends in a traceback.
        self.socket_path.unlink(missing_ok=True)

    # -- internals ---------------------------------------------------------

    async def _supervise(self) -> None:
        assert self._binary is not None
        backoff = BACKOFF_MIN

        while not self._stopping:
            started = time.monotonic()
            try:
                process = await self._spawn()
            except OSError as exc:
                # The binary was there when we looked and is not now, or is not
                # executable. Not fatal to the Controller: hosts that enrol
                # normally are unaffected.
                self._status = LocalAgentStatus(
                    LocalAgentState.UNAVAILABLE, f"cannot run {self._binary}: {exc}"
                )
                log.error("local agent: %s", self._status.detail)
                return

            self._process = process
            self._status = LocalAgentStatus(LocalAgentState.RUNNING, str(self._binary))
            log.info("local agent running (pid %d) against %s", process.pid, self.socket_path)

            exit_code, diagnosis = await asyncio.gather(process.wait(), self._relay(process))
            if self._stopping:
                return

            # A child that ran for a while and then died is a new failure, not
            # the continuation of an old one, and gets the short backoff back.
            healthy = time.monotonic() - started > HEALTHY_RUN
            if healthy:
                backoff = BACKOFF_MIN

            # For a child that never got going, the agent's own words. The most
            # common failure on this path is a user who is not in the `docker`
            # group, and "exited with status 1" in a fleet panel sends them to
            # a log file to find the sentence that says what to type.
            #
            # For one that ran and then stopped, the status: its first line was
            # "connected to …", which was true and is now the wrong thing to
            # show. How long it lived is what tells the two apart, rather than
            # anything about the text -- so this does not become a supervisor
            # that parses its child's log format.
            self._status = LocalAgentStatus(
                LocalAgentState.STARTING,
                f"agent exited with status {exit_code}; restarting"
                if healthy or not diagnosis
                else diagnosis,
            )
            log.warning(
                "local agent exited with status %s; restarting in %.0fs", exit_code, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _spawn(self) -> asyncio.subprocess.Process:
        assert self._binary is not None
        arguments = [
            "--controller",
            f"unix:{self.socket_path}",
            "--socket",
            str(self.engine_socket),
        ]
        # The same setting the command layer enforces, applied at the other
        # choke point (ARCHITECTURE §9). Passing it means a read-only
        # Controller's own agent refuses mutations even if something ever
        # reached it directly.
        if self._settings.read_only:
            arguments.append("--read-only")

        return await asyncio.create_subprocess_exec(
            str(self._binary),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # A deliberately empty environment. The agent reads `BYSTACK_*`
            # variables as configuration, and inheriting ours would let a
            # `BYSTACK_READ_ONLY` or `BYSTACK_CONTROLLER` set for something
            # else quietly redirect or unlock the Controller's own agent. Its
            # configuration is the argument list above, entirely.
            env={"PATH": os.environ.get("PATH", "")},
        )

    async def _relay(self, process: asyncio.subprocess.Process) -> str:
        """Re-emit the child's output, and keep its first line back.

        Inheriting the Controller's stdout would have been one line shorter and
        would put unprefixed prints in the middle of structured logs. It also
        matters more than it looks: the most common first-run failure on this
        path is a user who is not in the `docker` group, and the agent's
        message about it is the one that says what to type.

        The **first** line rather than the last, deliberately. The agent
        reports the diagnosis and then elaborates on it -- "cannot reach the
        docker socket: Permission denied", then the two lines of advice -- so
        the last line of a failing run is a shell command with no context.
        """
        stream = process.stdout
        if stream is None:
            return ""
        first = ""
        async for line in stream:
            text = line.decode("utf-8", "replace").rstrip().removeprefix("bystack-agent: ")
            if text:
                log.info("local agent | %s", text)
                first = first or text
        return first

    async def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE)
        except TimeoutError:
            log.warning("local agent did not stop in %.0fs; killing it", STOP_GRACE)
            process.kill()
            await process.wait()


def _resolve(configured: str, fallback: Path) -> Path:
    return Path(configured).expanduser() if configured else fallback


def find_binary(configured: str = "") -> tuple[Path | None, str]:
    """`_find_binary`, for callers outside this module.

    The deploy service needs the same answer this one does -- which agent
    binary this Controller holds -- and asking it twice in two ways is how the
    machine it spawns for itself and the machine it installs on somebody else
    end up on different builds.
    """
    return _find_binary(configured)


def _find_binary(configured: str) -> tuple[Path | None, str]:
    """Locate the agent, and say where we looked when we cannot.

    A configured path is an assertion and is not fallen back from: an operator
    who named a binary and got a different one silently would have no way to
    discover it.
    """
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            return None, f"local_agent.binary is set to {path}, which is not a file"
        if not os.access(path, os.X_OK):
            return None, f"{path} is not executable"
        return path, ""

    override = os.environ.get("BYSTACK_AGENT_BINARY")
    candidates = [Path(override).expanduser()] if override else []
    candidates.append(BUNDLED)
    found = shutil.which("bystack-agent")
    if found:
        candidates.append(Path(found))
    candidates.append(IN_TREE)

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate, ""

    return None, (
        "no bystack-agent binary found. Looked in "
        + ", ".join(str(candidate) for candidate in candidates)
        + ". Build it with `cargo build --release` in agent/, put it on PATH, "
        "or set local_agent.binary."
    )
