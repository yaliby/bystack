"""Installing an agent on a machine the operator named (ADR-0019).

The last manual step in the product. The Controller installs itself
(ADR-0018), upgrades itself, and upgrades every agent in the fleet without
anybody logging in (ADR-0017) — and then the *first* install on each host was
an ssh session and a paste. This is that step, done by the Controller, with a
credential it is not allowed to keep.

## What a run is

A list of addresses, one credential, and a sequence:

    connect → what is this machine → mint a token → put the agent there
            → run the installer → wait for it to dial back → next host

**One host at a time, and a failure stops the run.** The same shape as
ADR-0017's staged rollout and for the same reason: ten machines at once is
worth having only in proportion to the confidence that it works, and the first
one is the cheapest place to find out it does not.

Sequencing has a second consequence that is not cosmetic. Nothing in the
enrolment record says which token was redeemed, so there is no field mapping a
new agent back to the machine the operator typed. What makes the mapping sound
is that only one install is ever in flight: an enrolment that appears during
this host's window is this host's. Parallel installs would need a token→host
link inside a trust artifact to answer a question that sequencing answers for
free.

## What it forgets

The credential is held in one local variable for the length of the run and is
never written down. There is no host inventory, no stored key, and no
"reconnect". A run that has finished holds addresses, outcomes and engine ids
— the things already visible on the Hosts panel — and nothing that could open
a second connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from bystack.infra.ssh import Credential, Endpoint, KnownHosts, Session, SshError, connect

log = logging.getLogger(__name__)

#: How long a host has to dial back after the installer says it is done.
#:
#: Generous. The installer starts the unit and returns; what happens next is a
#: TLS handshake, an enrolment and a `Hello` — over whatever link that machine
#: has. The cost of waiting is a spinner; the cost of being impatient is
#: telling an operator a host failed while it is connecting.
ENROL_TIMEOUT = 120.0

#: How often to look for the new agent during that.
POLL_SECONDS = 1.0

#: Where the installer and the agent are put on the target, and why in /tmp:
#: it is the one directory every machine has and nothing here is meant to
#: outlive the run. The installer copies the binary into place itself.
REMOTE_INSTALLER = "/tmp/bystack-install-agent.sh"
REMOTE_AGENT = "/tmp/bystack-agent"

#: Long enough for a slow link to move a two-megabyte binary and for the
#: installer to wait out its own enrolment check, short enough that a wedged
#: machine does not hold a run open forever.
INSTALL_TIMEOUT = 600.0


class Phase(StrEnum):
    """Where one host has got to.

    A closed set, and the terminal ones are what the dashboard stops polling
    on. `skipped` is not a failure: a machine that already runs an agent is a
    correct outcome for "make this machine managed".
    """

    WAITING = "waiting"
    CONNECTING = "connecting"
    PREPARING = "preparing"
    INSTALLING = "installing"
    ENROLLING = "enrolling"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


TERMINAL = frozenset({Phase.DONE, Phase.SKIPPED, Phase.FAILED})


@dataclass
class HostState:
    """One machine's row, as the dashboard draws it."""

    host: str
    port: int = 22
    phase: Phase = Phase.WAITING
    detail: str = ""
    engine_id: str = ""
    fingerprint: str = ""
    """The host key this address answered with. Shown so an operator can
    compare it to what their own terminal prints, which is the only thing that
    makes trust-on-first-use mean anything."""

    @property
    def address(self) -> str:
        return self.host if self.port == 22 else f"{self.host}:{self.port}"


@dataclass
class RunState:
    """A deployment, in flight or finished."""

    started_at: int
    hosts: list[HostState]
    finished_at: int = 0
    error: str = ""

    @property
    def running(self) -> bool:
        return self.finished_at == 0

    @property
    def done(self) -> int:
        return sum(1 for host in self.hosts if host.phase in (Phase.DONE, Phase.SKIPPED))


@dataclass(frozen=True, slots=True)
class Target:
    host: str
    port: int = 22


class DeployError(RuntimeError):
    """Something the operator needs a sentence about."""


#: What the service needs from the rest of the Controller, as three callables.
#:
#: Passed rather than reached for, the way `SelfUpdateService` takes the
#: rollout rather than the upgrade service: this needs to mint a token, know
#: which engines are enrolled, and know where agents dial. Holding the trust
#: store and the collector would let it grow opinions about enrolment that
#: belong to ADR-0011.
MintToken = Callable[[dt.timedelta], "object"]
EnrolledIds = Callable[[], "set[str]"]


@dataclass
class Sources:
    """Where the bytes come from, in the order they are preferred.

    `releases_dir` first because those are *signed*: the installer checks them
    against a key compiled into itself, so an agent placed this way is trusted
    on the same terms as one pushed by ADR-0017's rollout. The bundled copy
    second, because a Controller always has one and it is better than making a
    small machine reach the internet. GitHub last, which is what the pasted
    command has always done and the only path that needs the host to have
    egress at all.
    """

    installer: Path | None
    releases_dir: Path
    bundled_agent: Path | None
    bundled_arch: str

    def agent_for(self, arch: str) -> tuple[Path, list[Path]] | None:
        """The best agent this Controller can hand a host of `arch`."""
        binary = self.releases_dir / f"bystack-agent-{arch}"
        manifest = binary.with_name(binary.name + ".manifest")
        signature = binary.with_name(binary.name + ".manifest.sig")
        if binary.is_file() and manifest.is_file() and signature.is_file():
            return binary, [manifest, signature]
        if (
            self.bundled_agent is not None
            and arch == self.bundled_arch
            and self.bundled_agent.is_file()
        ):
            return self.bundled_agent, []
        return None


class DeployService:
    """One run at a time, and nothing kept between them."""

    __slots__ = ("_sources", "_known_hosts", "_mint", "_enrolled", "_dial_url", "_read_only",
                 "_run", "_task", "_connect")

    def __init__(
        self,
        *,
        sources: Sources,
        known_hosts: KnownHosts,
        mint_token: Callable[[dt.timedelta], object],
        enrolled_ids: Callable[[], set[str]],
        dial_url: Callable[[], str],
        read_only: bool = False,
        connector: Callable[..., Awaitable[object]] | None = None,
    ) -> None:
        self._sources = sources
        self._known_hosts = known_hosts
        self._mint = mint_token
        self._enrolled = enrolled_ids
        self._dial_url = dial_url
        self._read_only = read_only
        self._run: RunState | None = None
        self._task: asyncio.Task[None] | None = None
        #: The seam the tests use. Production is `infra.ssh.connect`, and the
        #: fake is a dictionary -- see the module docstring in `infra/ssh.py`
        #: for why the decisions live here and the socket lives there.
        self._connect = connector or connect

    # -- what the routes see ----------------------------------------------

    def status(self) -> RunState | None:
        return self._run

    def start(self, targets: Sequence[Target], credential: Credential) -> RunState:
        """Begin a run, or say why not. Returns immediately.

        The credential reaches exactly one place from here: the local variable
        in `_run_all` that the connection is opened with. It is not stored on
        `self`, so a run that has finished leaves nothing behind that a later
        request could reuse — which is the property, rather than a policy
        anybody has to remember.
        """
        if self._read_only:
            raise DeployError(
                "this Controller is read-only. Installing a root daemon on a machine "
                "that did not have one is the largest thing it can be asked to do to "
                "somebody else's computer, so it is one of the things that mode is for."
            )
        if self._run is not None and self._run.running:
            raise DeployError(
                "a deployment is already running. One at a time: each host is confirmed "
                "before the next is touched, which is also what tells us which new agent "
                "is which."
            )
        if not targets:
            raise DeployError("no hosts were given")
        if not credential.valid:
            raise DeployError(
                "a user and either a password or a private key are needed. The agent "
                "installs as a system service, so this has to be root or an account "
                "that can sudo without a password."
            )

        seen: set[str] = set()
        hosts: list[HostState] = []
        for target in targets:
            state = HostState(host=target.host, port=target.port)
            if state.address in seen:
                continue
            seen.add(state.address)
            hosts.append(state)

        self._run = RunState(started_at=int(time.time()), hosts=hosts)
        self._task = asyncio.create_task(
            self._run_all(self._run, credential), name="bystack-deploy"
        )
        return self._run

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- the run -----------------------------------------------------------

    async def _run_all(self, run: RunState, credential: Credential) -> None:
        try:
            for host in run.hosts:
                try:
                    await self._one(host, credential)
                except SshError as exc:
                    host.phase, host.detail = Phase.FAILED, str(exc)
                except DeployError as exc:
                    host.phase, host.detail = Phase.FAILED, str(exc)
                except Exception as exc:  # noqa: BLE001 - a row, not a 500
                    log.exception("deploying to %s failed", host.host)
                    host.phase, host.detail = Phase.FAILED, f"unexpected failure: {exc}"

                if host.phase is Phase.FAILED:
                    # The run stops. Every host after this one keeps `waiting`,
                    # which is the honest word for what happened to it: not
                    # attempted, not failed.
                    run.error = f"{host.address}: {host.detail}"
                    break
        finally:
            run.finished_at = int(time.time())

    async def _one(self, host: HostState, credential: Credential) -> None:
        host.phase, host.detail = Phase.CONNECTING, f"Opening a connection to {host.host}."
        opened = await self._connect(
            Endpoint(host.host, host.port), credential, self._known_hosts
        )
        session: Session = opened.session  # type: ignore[attr-defined]
        host.fingerprint = opened.fingerprint  # type: ignore[attr-defined]
        try:
            await self._install(host, session)
        finally:
            await session.close()

    async def _install(self, host: HostState, session: Session) -> None:
        host.phase, host.detail = Phase.PREPARING, "Looking at the machine."

        # Root, or a sudo that needs no password. Checked before anything is
        # uploaded: an operator who gave an unprivileged account should be told
        # so in a sentence rather than by an installer refusing half-way.
        sudo = await self._privilege(session)

        already = await session.run("systemctl is-active bystack-agent 2>/dev/null || true")
        if already.stdout.strip() == "active":
            host.phase = Phase.SKIPPED
            host.detail = (
                "This machine already runs an agent, so nothing was installed. "
                "Upgrades go down the connection it already holds."
            )
            return

        # Installed, and not running. The machine still has its *identity*:
        # `install-agent.sh` never touches the certificate directory, because
        # that certificate is what the Controller knows this host as.
        #
        # Which changes what this run is, in two ways that both matter:
        #
        #  * it must not carry a token. A token beside a stored certificate
        #    makes the agent enrol again and come back as a stranger awaiting
        #    approval, while the host you actually have goes quiet -- the exact
        #    trap INSTALL.md documents for the pasted upgrade line (ADR-0011).
        #  * it cannot be confirmed by a *new* enrolment, because there will
        #    not be one. Waiting for one is a run that reports a failure two
        #    minutes after the thing it was asked to do has worked.
        enrolled_here = await session.run(
            "test -f /var/lib/bystack-agent/agent.crt && echo yes || true"
        )
        repair = enrolled_here.stdout.strip() == "yes"

        arch_result = await session.run("uname -m")
        if not arch_result.ok:
            raise DeployError(f"cannot tell what this machine is: {arch_result.failure()}")
        arch = _normalise_arch(arch_result.stdout.strip())

        if self._sources.installer is None:
            raise DeployError(
                "this Controller does not carry install-agent.sh, so it has nothing to "
                "run on that machine. Use the command in the dialog instead."
            )
        await session.upload(
            self._sources.installer.read_bytes(), REMOTE_INSTALLER, mode=0o700
        )

        binary_flag = ""
        agent = self._sources.agent_for(arch)
        if agent is not None:
            binary, extras = agent
            await session.upload(binary.read_bytes(), REMOTE_AGENT, mode=0o700)
            for extra in extras:
                suffix = extra.name[len(binary.name):]
                await session.upload(extra.read_bytes(), REMOTE_AGENT + suffix, mode=0o600)
            binary_flag = f" --binary {REMOTE_AGENT}"
            host.detail = (
                f"Sent the {arch} agent"
                + (" and its signature." if extras else " (unsigned, from this Controller).")
            )
        else:
            host.detail = (
                f"This Controller holds no {arch} agent, so that machine will fetch one "
                f"from GitHub. It needs outbound access for this step only."
            )

        before = self._enrolled()
        credential_flag = ""
        if not repair:
            minted = self._mint(dt.timedelta(minutes=15))
            token = getattr(minted, "token", "")
            if not token:  # pragma: no cover - a broken trust store
                raise DeployError("could not mint a join token")
            credential_flag = f" --token {shlex.quote(token)}"

        host.phase = Phase.INSTALLING
        command = (
            f"{sudo}{REMOTE_INSTALLER} --controller {shlex.quote(self._dial_url())}"
            f"{credential_flag}{binary_flag}"
        )
        try:
            result = await asyncio.wait_for(session.run(command), timeout=INSTALL_TIMEOUT)
        except TimeoutError as exc:
            raise DeployError(
                f"the installer did not finish within {int(INSTALL_TIMEOUT)}s. The agent "
                f"may still come up; the Hosts panel is where it would appear."
            ) from exc
        finally:
            # The token is single-use and short-lived, and the binary is a
            # public artifact -- but neither belongs in /tmp on somebody's
            # server after the job is done, and a failed run is exactly when
            # nobody goes back to look.
            await session.run(f"rm -f {REMOTE_INSTALLER} {REMOTE_AGENT} {REMOTE_AGENT}.manifest*")

        if not result.ok:
            raise DeployError(await self._diagnose(session, result.failure()))

        host.phase = Phase.ENROLLING
        host.detail = "Installed. Waiting for it to dial in."
        if repair:
            # It comes back as the host it always was, so the thing to wait for
            # is the unit, not the registry.
            if not await self._await_unit(session):
                raise DeployError(await self._diagnose(session, "it did not come back up"))
            host.phase = Phase.DONE
            host.detail = (
                "Reinstalled, and running again. It keeps the identity it already had, "
                "so it comes back as the host you know rather than as a new one."
            )
            return
        engine = await self._await_enrolment(before)
        if engine is None:
            raise DeployError(
                f"the agent was installed and had not connected within "
                f"{int(ENROL_TIMEOUT)}s. It keeps trying, so it may still appear on the "
                f"Hosts panel; if it does not, the address it was given "
                f"({self._dial_url()}) is the first thing to check from that machine."
            )
        host.engine_id = engine
        host.phase = Phase.DONE
        host.detail = "Connected."

    async def _diagnose(self, session: Session, installer_said: str) -> str:
        """Ask the machine why, rather than repeating what the installer said.

        The installer's last words when an agent does not come up are about
        the *token* -- it keeps it in place so a retry can use it -- and those
        lines are the tail of the output, so they are what a naive report
        shows. They are true and they are not the reason.

        The reason is in the unit's own journal, one command away, on a
        connection that is still open. The commonest first-install failure by
        a wide margin is a host with no container engine, and "cannot reach the
        docker socket" is a sentence an operator acts on where "the token is
        still in agent.env" is one they read twice and ignore.
        """
        journal = await session.run(
            "journalctl -u bystack-agent -n 12 --no-pager 2>/dev/null | tail -n 6"
        )
        said = journal.stdout.strip()
        if not said:
            return installer_said
        # The journal's own prefix is a timestamp, a hostname and the unit --
        # thirty characters of nothing, three times over, in a message shown
        # in a table cell.
        lines = [
            line.split("]: ", 1)[-1].strip() for line in said.splitlines() if ": " in line
        ]
        interesting = [line for line in lines if line and not line.startswith("Start")]
        if not interesting:
            return installer_said
        return f"the agent was installed and will not run: {' '.join(interesting[-2:])}"

    async def _privilege(self, session: Session) -> str:
        """`""` for root, `"sudo -n "` for an account that can, or a refusal.

        `sudo -n` rather than a password prompt on purpose. A password this
        service asked for interactively would be a second credential to carry
        through the run, and a sudo that *asks* is a command that hangs
        forever on a machine nobody is watching.
        """
        who = await session.run("id -u")
        if who.ok and who.stdout.strip() == "0":
            return ""
        can = await session.run("sudo -n true 2>/dev/null && echo yes || true")
        if can.stdout.strip() == "yes":
            return "sudo -n "
        raise DeployError(
            "that account is not root and cannot sudo without a password. The agent "
            "installs as a system service, so it needs one or the other."
        )

    async def _await_unit(self, session: Session) -> bool:
        """Whether `bystack-agent` is running, given a moment to settle.

        What a repair is confirmed by. The unit restarts on a backoff, so a
        single check immediately after the installer returns is a coin toss --
        and the agent's own first action is a network handshake.
        """
        deadline = time.monotonic() + ENROL_TIMEOUT
        while time.monotonic() < deadline:
            state = await session.run("systemctl is-active bystack-agent 2>/dev/null || true")
            if state.stdout.strip() == "active":
                return True
            await asyncio.sleep(POLL_SECONDS)
        return False

    async def _await_enrolment(self, before: set[str]) -> str | None:
        """The engine id that appeared while this host was installing.

        Sound because the run is sequential -- see the module docstring. An
        `auto_approve` fleet and one that waits for a click both land here:
        what is being waited for is the *enrolment*, which happens on the
        agent's first connection, not the approval that may follow it.
        """
        deadline = time.monotonic() + ENROL_TIMEOUT
        while time.monotonic() < deadline:
            appeared = self._enrolled() - before
            if appeared:
                return sorted(appeared)[0]
            await asyncio.sleep(POLL_SECONDS)
        return None


def _normalise_arch(reported: str) -> str:
    """`uname -m`, in the spelling the release artifacts are named with."""
    value = reported.strip().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)


def parse_hosts(text: str) -> list[Target]:
    """A textarea into targets, forgivingly.

    Newlines, commas and spaces all separate, because an operator pasting from
    an inventory has whichever one that inventory used and none of them is
    wrong. `host:port` is honoured; a bare IPv6 address is not guessed at --
    `[::1]:22` is the spelling that is unambiguous, and it is the one every
    other tool wants too.
    """
    targets: list[Target] = []
    for raw in _split(text):
        host, port = raw, 22
        if raw.startswith("["):
            closing = raw.find("]")
            if closing == -1:
                raise DeployError(f"{raw!r} opens a bracket and does not close it")
            host = raw[1:closing]
            rest = raw[closing + 1:]
            if rest.startswith(":"):
                port = _port(rest[1:], raw)
        elif raw.count(":") == 1:
            host, _, tail = raw.partition(":")
            port = _port(tail, raw)
        if not host:
            raise DeployError(f"{raw!r} names no host")
        targets.append(Target(host=host, port=port))
    return targets


def _split(text: str) -> Iterable[str]:
    """Line by line, because a `#` comments out the *rest of its line*.

    Splitting the whole document on whitespace first is the version of this
    that looks equivalent and is not: `# staging box` would contribute two
    hosts called `staging` and `box`, and the operator would watch the run try
    to connect to them.
    """
    for line in text.splitlines():
        body, _, _comment = line.partition("#")
        for chunk in body.replace(",", " ").split():
            piece = chunk.strip()
            if piece:
                yield piece


def _port(value: str, whole: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise DeployError(f"{whole!r} does not end in a port number")
    return int(value)
