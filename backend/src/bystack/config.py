"""Configuration.

Manual configuration exists only to tell the platform *where to look*. It
never describes topology -- that is discovered. The golden rule holds: a user
who has to hand-draw their infrastructure is using a diagramming tool, not a
control plane.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

#: What a `hosts:` block used to mean, and why it is now refused.
#:
#: `docs/MIGRATION.md` §6 asked for a hard error rather than a silent
#: migration, and the reason is the whole point: a config that quietly
#: degraded to "manage nothing" looks exactly like an empty cluster. An
#: operator upgrading with a `hosts:` block would see a Controller that
#: started cleanly, reported healthy, and discovered nothing at all.
_HOSTS_REMOVED = """\
`hosts:` is no longer supported. The Controller does not reach out to Docker
sockets any more -- agents dial in (ARCHITECTURE.md section 1).

Install an agent on each host listed there and enable the endpoint:

    agents:
      enabled: true

See docs/MIGRATION.md for what moved and why."""

#: Where a Controller keeps the things it must not lose: the CA private key,
#: the enrollment registry, the audit log.
#:
#: A module constant rather than a literal in the field, so that a test suite
#: can redirect every default at once. That is not cosmetic — several tests
#: build an app from bare `Settings()`, and without one place to move, they
#: create a CA and append to an audit log in the home directory of whoever is
#: running them. The rule in this repo is that no test touches anything
#: outside its own temporary directory, and a default spelled inline is a
#: quiet exception to it.
DEFAULT_STATE_DIR = "~/.local/state/bystack"


class AgentsConfig(BaseModel):
    """Where agents dial in, and on what terms.

    Says far less than the ``hosts:`` block it will replace, because agents
    *arrive* rather than being reached. There is nothing here about addresses,
    credentials or transports -- that whole category of configuration is what
    ADR-0008 deleted.
    """

    enabled: bool = Field(
        default=False,
        description="Accept agent connections. Off until an operator opts in.",
    )
    """Off until asked, though no longer because we cannot tell who is calling.

    Every connection is mutually authenticated now (ADR-0011), so an open
    listener is not the exposure it was before step 4. What is left is
    narrower and still worth defaulting away from: this binds a port, usually
    a public one, and a Controller someone started to look at a graph should
    not open it without being asked.
    """

    listen: str = Field(
        default="0.0.0.0:8443",
        description="host:port where agents dial in. Separate from the UI port.",
    )
    """A separate listener from the browser's, deliberately (MIGRATION section 3).

    One side is browser-facing and may sit behind an ordinary reverse proxy;
    this one requires client certificates and must not be terminated by
    anything that would strip them. Two ports is the honest way to say that.

    `0.0.0.0` because an agent listener that only accepts loopback accepts no
    agents -- the whole model is remote hosts dialling in. The exposure is
    bounded by the fact that nothing without a certificate from our CA
    completes a handshake.
    """

    server_names: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1"],
        description="Names and addresses the listener's certificate is valid for.",
    )
    """What agents will have typed after `wss://`.

    This has to be configured for any deployment agents reach by a real name:
    a certificate without the name in it fails verification, correctly, and
    the agent will say so. Defaulted to loopback rather than to a guess,
    because a guessed hostname produces a certificate that is wrong in a way
    that looks like a bug in the CA.
    """

    auto_approve: bool = Field(
        default=False,
        description="Approve an agent on enrollment instead of leaving it pending.",
    )
    """Off by default, and the default is the security control.

    A valid join token always yields a certificate; approval is the separate
    question of whether that agent contributes to the graph. Leaving it off
    means a stolen token produces a visible pending agent instead of a silent
    managed host -- and that visibility is the entire point (ADR-0011).

    With it on, whatever redeems a token becomes a managed host immediately.
    Reasonable while bringing up a fleet, never as a shipped default.
    """

    cert_ttl_days: int = Field(
        default=90,
        ge=1,
        le=825,
        description="Lifetime of an issued agent certificate.",
    )
    """Short, and renewed at two thirds elapsed over the stream that is already
    open. No cron job, no second channel, no expiry outage. The ceiling is the
    825 days browsers settled on; nothing here needs to go near it."""

    max_clock_skew: int = Field(
        default=60,
        ge=1,
        description="Seconds of disagreement with an agent's clock we tolerate.",
    )
    """Certificate validation is time-sensitive, so a badly wrong clock is a
    connection failure -- and one that surfaces as a generic TLS error saying
    nothing useful. The agent reports its own clock in `Hello` so we can refuse
    with the actual diagnosis while its certificate is still valid."""

    state_dir: str = Field(
        default_factory=lambda: DEFAULT_STATE_DIR,
        description="Where the CA key and the enrollment registry live.",
    )
    """The one directory in this project worth backing up.

    It holds the CA private key -- losing it means re-enrolling the fleet --
    and the enrollment registry, which is the operator's record of which hosts
    were approved. Everything else the Controller knows is rebuilt from the
    agents within seconds of a cold start (ADR-0001).
    """

    releases_dir: str = Field(
        default="",
        description="Signed agent releases to distribute. Empty means <state_dir>/releases.",
    )
    """Where the artifacts a fleet is upgraded from live (ADR-0017).

    A directory of `bystack-agent-<arch>` files, each with the
    `.manifest` and `.manifest.sig` that make it worth installing. The
    Controller holds **no signing key** and cannot produce any of them: it
    picks the artifact matching each host and sends it, and the host decides
    whether to run it against a key compiled into the agent.

    That is what makes *how the files got here* an operational question rather
    than a trust decision. Fetched from GitHub Releases, copied off a laptop,
    dropped in by configuration management -- all equivalent, because none of
    them is what the agent is relying on.

    An empty directory is the ordinary state and not a fault: it means this
    Controller distributes nothing and hosts are upgraded by running the
    installer on them, which is what every host does for its first install
    anyway.
    """

    @property
    def releases_path(self) -> str:
        """`releases_dir`, defaulted into the state directory.

        Beside the CA and the enrollment registry rather than in a second
        place, for the reason the audit log is: that directory is already the
        one an operator knows about, already created, and already has the right
        permissions. Unlike the things beside it, this one is *not* worth
        backing up -- every file in it is a public artifact that can be
        downloaded again.
        """
        return self.releases_dir or f"{self.state_dir.rstrip('/')}/releases"

    @property
    def listen_address(self) -> tuple[str, int]:
        """``listen`` split, with ``[::]:8443`` spelled the way it must be.

        Validated on load rather than at bind time: a port typo that surfaces
        when uvicorn starts is a stack trace, and one that surfaces here is a
        message naming the field.
        """
        return _split_address(self.listen)

    @field_validator("listen")
    @classmethod
    def _validate_listen(cls, value: str) -> str:
        _split_address(value)
        return value

    resync_interval: int = Field(
        default=900,
        ge=60,
        description="Seconds between agent-side resyncs; sent in HelloAck.",
    )
    """15 minutes, three times the agentless interval.

    Resync ran every 5 minutes because the event stream crossed an SSH tunnel
    that could drop silently. An agent reads a unix socket on the same kernel;
    there is no network to lose. It now defends only against agent-side hash
    map bugs, and at steady state it produces zero bytes on the wire because
    nothing changed and therefore nothing is sent.
    """


class LocalAgentConfig(BaseModel):
    """The agent the Controller spawns for its own machine.

    This is what zero-config startup is (`docs/MIGRATION.md` §4). It is not a
    second discovery path: the Controller runs the same agent binary every
    managed host runs, over a unix socket instead of TLS, and everything above
    the transport — informer, frames, ingest, partition writer — is the code
    that serves the fleet.
    """

    enabled: bool = Field(
        default=True,
        description="Manage this machine's Docker engine by spawning a local agent.",
    )
    """On by default, unlike `agents.enabled`, and for the reason that one is
    off: this binds no port and admits no stranger. It costs a 1.8 MiB child
    process reading a socket that is already on this machine.

    When there is no engine here -- a Controller on a host that manages
    others -- it finds no Docker socket and says so, which is a better first
    run than an empty graph with no explanation.
    """

    binary: str = Field(
        default="",
        description="Path to bystack-agent. Empty means find it.",
    )
    """Empty searches `BYSTACK_AGENT_BINARY`, the bundled copy, `PATH`, and the
    build directory of a source checkout, in that order. Set explicitly it is
    an assertion and is never fallen back from -- an operator who named a
    binary and silently got a different one would have no way to find out."""

    docker_socket: str = Field(
        default="/var/run/docker.sock",
        min_length=1,
        description="The engine the local agent reads. Never read by the Controller.",
    )
    """Required, unlike the two paths above, because there is no sensible way to
    search for it and an empty string is not one: `Path("")` is the current
    directory, which exists -- so a blank value would sail through the "is
    there an engine here" check and spawn an agent pointed at a directory.
    Refused at load, where the message can name the field."""
    """The Controller does not open this and never will (ARCHITECTURE §1); it
    is passed to the child, which is the only process here that speaks to
    Docker at all."""

    socket: str = Field(
        default="",
        description="Where the local agent dials. Empty means inside agents.state_dir.",
    )
    """Created 0600 in a 0700 directory, which is the whole of this
    connection's authentication: anything that can open it is already this
    user on this machine, with the access to the Docker socket that an agent
    would be protecting."""


class AuditConfig(BaseModel):
    """Where the record of every attempted operation goes, and how much of it is kept.

    Durable by default, and that is a change of posture rather than a
    convenience. A log a restart erases cannot answer "who did this", and
    ADR-0012 puts that question in front of every destructive verb. A control
    plane
    holding root-equivalent access to a fleet should not have to be configured
    into remembering what it was asked to do.

    It answers "what was attempted, and when", and deliberately not "by whom":
    `actor` is the string "anonymous" permanently, because ADR-0014 decides
    this platform does not identify its operator. The verbs it records cannot
    destroy anything, which is the other half of that decision.
    """

    durable: bool = Field(
        default=True,
        description="Write the audit log to disk. Off keeps the bounded in-memory ring.",
    )

    path: str = Field(
        default="",
        description="Directory for the audit log. Empty means inside agents.state_dir.",
    )
    """Defaulted into the state directory rather than beside it.

    That directory is already the one documented as worth backing up, is
    already created 0700, and is already the answer to "where does this
    Controller keep the things it must not lose". A second location would be
    a second thing to find, back up and get the permissions right on.
    """

    retain: int = Field(
        default=20_000,
        ge=1,
        description="Operations kept. The oldest are dropped when the log is compacted.",
    )
    """ADR-0006 clause 4: every table has a retention policy, and this is it.

    By count rather than by age, deliberately. "Ninety days" is what an
    auditor asks for and the wrong thing to implement first: it makes the
    file's size a function of how busy the installation is, which is the
    unbounded growth the in-memory version was careful to avoid. A count is a
    bound; a duration is a hope.
    """


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    """Loopback by default. This service holds root-equivalent access to every
    managed engine; binding it to the world on first run is not a default any
    platform should ship.

    **This bind is the whole of the browser-side authorization** (ADR-0014).
    Nothing authenticates a browser to this API by design, so anyone who can
    reach this port can start, stop, restart and kill containers on every
    managed host -- and since ADR-0014 they can do it out of the box, because
    `read_only` now defaults to `False`. What bounds the damage is the verb set
    rather than the flag: `CommandKind` holds nothing that destroys anything,
    so the worst case is a container restarting. That is the reason changing
    this to `0.0.0.0` is a decision about how much you trust the network, not a
    convenience. Put a reverse proxy with authentication in front of it if the
    answer is "not much"; the browser port was deliberately kept able to sit
    behind an ordinary one."""

    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


class Settings(BaseModel):
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    local_agent: LocalAgentConfig = Field(default_factory=LocalAgentConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    read_only: bool = Field(
        default=False,
        description="Refuse all mutating operations. On makes this a viewer.",
    )
    """Off by default, because this is a control plane and not a diagram.

    It shipped `True` on the reasoning that a mutating default is never safe.
    That reasoning assumed a verb set that could hurt you, and ADR-0014 closed
    that door permanently: everything in `CommandKind` is a reversible
    lifecycle transition, the API binds to loopback, and every attempt is
    recorded durably. The worst a wrong click does is restart a container --
    recoverable, audited, and visible on the canvas a second later.

    Against that, the cost of the safe default was real and was paid on every
    first run: a platform whose entire point is *"the live map is the control
    surface"* came up refusing every action, with no error a new operator could
    connect to a setting they had never read about. A default that makes the
    product look broken is not a secure default, it is a support burden that
    teaches people to flip flags they have not understood.

    Set `read_only: true` to get the viewer back -- for a Controller pointed at
    production from a laptop, or any host you want to watch and not touch. The
    refusal is still enforced at one choke point (`CommandService`) and by each
    agent on its own authority, so turning it on is a real guarantee and not a
    UI preference.
    """
    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _refuse_removed_keys(cls, data: Any) -> Any:
        # Before-mode, so this fires on the raw mapping. Pydantic ignores
        # unknown keys by default, which is exactly the silent degradation
        # MIGRATION section 6 asked us not to ship.
        if isinstance(data, dict) and "hosts" in data:
            raise ValueError(_HOSTS_REMOVED)
        return data

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        """Load from YAML, failing loudly on a malformed file.

        No silent fallback to defaults: a typo in a config block that quietly
        degraded to "manage nothing" would look identical to an empty cluster,
        and the user would have no way to tell.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")

        raw: Any = yaml.safe_load(path.read_text()) or {}
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"invalid configuration in {path}:\n{exc}") from exc

    @classmethod
    def default(cls) -> Settings:
        """Zero-config startup: manage this machine, and wait for the rest.

        It means "discover the local engine" again, and it does so without the
        Controller reaching a Docker socket: a local agent is spawned and dials
        in over a unix socket, through the same ingest path every enrolled host
        uses (`runtime/localagent.py`, `docs/MIGRATION.md` section 4).

        The fleet listener stays off until an operator turns it on. That is a
        different question and keeps its answer: it binds a port, usually a
        public one, and a Controller started to look at a graph should not open
        one unasked.
        """
        return cls()


def _split_address(value: str) -> tuple[str, int]:
    """``host:port``, including the bracketed IPv6 spelling."""
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise ValueError(f"expected host:port, got {value!r}")
    host = host.strip("[]")
    try:
        number = int(port)
    except ValueError:
        raise ValueError(f"{port!r} is not a port number in {value!r}") from None
    if not 1 <= number <= 65535:
        raise ValueError(f"port {number} is out of range in {value!r}")
    return host, number
