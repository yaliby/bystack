"""Composition root.

The only module permitted to know about every layer at once. Everything else
receives what it needs through a constructor or a dependency, which is what
keeps the dependency graph acyclic and every module independently testable.
"""

from __future__ import annotations

import logging
import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bystack import __version__
from bystack.api.deps import AppContext
from bystack.api.routes import agents as agent_routes
from bystack.api.routes import commands as commands_routes
from bystack.api.routes import (
    controller,
    deploy,
    enrollment,
    graph,
    health,
    stream,
    upgrades,
    watch,
)
from bystack.api.web import mount_web
from bystack.config import Settings
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.ports.command import AuditLog
from bystack.core.ports.watch import WatchStore
from bystack.infra.audit.durable import DurableAuditLog
from bystack.infra.audit.memory import InMemoryAuditLog
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.infra.manager import ManagerLink
from bystack.infra.releases import ReleaseStore
from bystack.infra.ssh import KnownHosts
from bystack.infra.watch.durable import DurableWatchStore
from bystack.infra.watch.memory import InMemoryWatchStore
from bystack.runtime.collector import Collector
from bystack.runtime.commands import CommandService
from bystack.runtime.deploy import DeployService, Sources
from bystack.runtime.localagent import BUNDLED_INSTALLER, IN_TREE_INSTALLER, LocalAgent
from bystack.runtime.localagent import find_binary as find_agent
from bystack.runtime.selfupdate import SelfUpdateService
from bystack.runtime.trust import AgentTrust
from bystack.runtime.upgrade import UpgradeService

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

#: `platform.machine()` spells two architectures differently from `uname -m`,
#: which is what the agent artifacts are named after and what a target machine
#: reports. Only the disagreements are listed; the same table is in
#: `hatch_build.py` and in `runtime/deploy.py`, because each is answering the
#: question for a different machine.
_ARCH_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}


def build_context(settings: Settings) -> AppContext:
    """Wire the object graph.

    Swapping any of these for another implementation of the same port -- a
    Redis bus, an RPC-backed store when the collector moves to its own
    process -- is a change to this function and nowhere else.
    """
    store = InMemoryGraphStore()
    bus = InMemoryEventBus()
    collector = Collector.from_settings(settings, store, bus)
    # Built here rather than inside the service so that the one object holding
    # a CA is created once and shared, the way the listener and the API share
    # it: `deploy` mints a join token like any other caller of this.
    trust = AgentTrust.from_settings(settings)
    commands = CommandService(
        store,
        _audit(settings),
        # A lookup closure rather than the collector itself: the command
        # service needs to resolve one provider by partition key and nothing
        # else, and handing it the supervisor would let it grow a dependency
        # on provider lifecycle that it has no business having.
        lambda source: collector.providers.get(source),
        read_only=settings.read_only,
    )
    return AppContext(
        settings=settings,
        store=store,
        bus=bus,
        collector=collector,
        commands=commands,
        # Opened whether or not the listener is enabled: minting a token is
        # how an operator gets to the point of enabling it, and the CA has to
        # exist before there is a fingerprint to put in one.
        trust=trust,
        # Constructed whether or not it can run, so that a Controller which
        # could not spawn one can say why rather than showing an empty fleet.
        local_agent=LocalAgent(settings),
        watchlist=_watchlist(settings),
        # The same lookup closure the command service takes, for the same
        # reason: a rollout enumerates providers and does nothing else with
        # the collector.
        upgrades=UpgradeService(
            ReleaseStore(settings.agents.releases_path),
            lambda: collector.providers,
            read_only=settings.read_only,
        ),
        selfupdate=SelfUpdateService(
            ManagerLink(settings.agents.manager_ipc_path),
            version=__version__,
            # Loopback, never `api.host`. A Controller bound to `0.0.0.0` still
            # answers here, and the manager refuses a health URL that is not on
            # this machine -- a probation check that can be pointed elsewhere is
            # one that always passes (ADR-0018).
            health=f"http://127.0.0.1:{settings.api.port}{API_PREFIX}/healthz",
            state_dir=settings.agents.state_dir,
            read_only=settings.read_only,
        ),
        deploy=DeployService(
            sources=_deploy_sources(settings),
            known_hosts=KnownHosts(Path(settings.agents.state_dir).expanduser() / "known_hosts"),
            # Three callables rather than the trust store, the registry and
            # the settings. This needs to mint a token, ask which engines are
            # enrolled, and know where agents dial; holding the CA would let a
            # deployment service grow opinions about enrolment that belong to
            # ADR-0011.
            mint_token=trust.mint_token,
            enrolled_ids=lambda: {agent.engine_id for agent in trust.registry.all()},
            dial_url=lambda: settings.agents.dial_url,
            read_only=settings.read_only,
        ),
    )


def _deploy_sources(settings: Settings) -> Sources:
    """What this Controller can put on a machine without it reaching GitHub.

    The signed releases first, then its own bundled agent, then nothing --
    which is not a failure, it is the case where the host fetches its own
    binary exactly as the pasted command has always made it do (ADR-0019).
    """
    installer = BUNDLED_INSTALLER if BUNDLED_INSTALLER.is_file() else None
    if installer is None and IN_TREE_INSTALLER.is_file():
        installer = IN_TREE_INSTALLER
    agent, _ = find_agent(settings.local_agent.binary)
    return Sources(
        installer=installer,
        releases_dir=Path(settings.agents.releases_path).expanduser(),
        bundled_agent=agent,
        bundled_arch=_ARCH_ALIASES.get(platform.machine().lower(), platform.machine().lower()),
    )


def _watchlist(settings: Settings) -> WatchStore:
    """The operator's selection of units and processes, durable if it can be.

    Same fallback as the audit log and for the same reason, with one
    difference worth stating: losing this is more visible than losing that. An
    audit ring that forgets costs history nobody is reading at the time; a
    watch list that forgets costs the operator the six services they chose,
    and the only symptom is a map that is missing things. So the failure is
    logged at the moment it is *set up*, rather than at the moment it matters,
    which is a restart away.
    """
    try:
        return DurableWatchStore.open(settings.agents.state_dir)
    except OSError as exc:
        log.error(
            "could not open the watch list in %s (%s); "
            "selections will be kept in memory and lost on restart",
            settings.agents.state_dir, exc,
        )
        return InMemoryWatchStore()


def _audit(settings: Settings) -> AuditLog:
    """The audit log, durable unless an operator turned that off.

    Falls back to the in-memory ring when the directory cannot be opened
    rather than refusing to start. That is the opposite of the enrollment
    registry's rule and the difference is what the failure costs: an
    unreadable allow-list means we do not know who is approved and every way
    to proceed is an outage, whereas an unwritable audit directory means we
    keep a shorter memory of what we were asked to do. Refusing to boot a
    control plane over the second would be a worse answer than saying so
    loudly and running.
    """
    if not settings.audit.durable:
        return InMemoryAuditLog()

    directory = settings.audit.path or settings.agents.state_dir
    try:
        return DurableAuditLog.open(directory, settings.audit.retain)
    except OSError as exc:
        log.error(
            "could not open the audit log in %s (%s); "
            "falling back to the in-memory ring, which a restart erases",
            directory, exc,
        )
        return InMemoryAuditLog()


def create_app(settings: Settings | None = None, context: AppContext | None = None) -> FastAPI:
    settings = settings or Settings.default()
    context = context or build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Discovery starts with the app and is never blocked on by it: an
        # unreachable host must leave the API up and reporting DEGRADED
        # rather than stalling startup.
        await context.collector.start()
        log.info("collector started with %d provider(s)", len(context.collector.providers))
        # Phase two of ADR-0018, and only ever for an update *this machine*
        # just performed: if the local updater installed this exact version and
        # left signed agents beside it, roll the fleet forward. The window is
        # bounded and the run is recorded, so a restart is not a second
        # rollout.
        context.selfupdate.start(
            lambda version: context.upgrades.start(version)  # type: ignore[arg-type,return-value]
        )
        try:
            yield
        finally:
            # Before the collector: a rollout in flight is holding a provider
            # and waiting on a host, and stopping the providers underneath it
            # would leave it waiting out a deadline for an answer that cannot
            # arrive.
            await context.selfupdate.aclose()
            await context.upgrades.aclose()
            await context.collector.stop()
            await context.bus.aclose()

    app = FastAPI(
        title="ByStack Control Plane",
        version=__version__,
        summary="Infrastructure discovery, correlation and operations for Docker",
        lifespan=lifespan,
    )
    app.state.context = context

    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(graph.router, prefix=API_PREFIX)
    app.include_router(stream.router, prefix=API_PREFIX)
    app.include_router(commands_routes.router, prefix=API_PREFIX)
    # Minting tokens and approving hosts, on the operator's side of the split.
    # The agent's side is `create_agent_app` and shares nothing but the
    # context.
    app.include_router(enrollment.router, prefix=API_PREFIX)
    # Before the routes that end in a path parameter, so `/agents/upgrades`
    # cannot be matched as an engine id by something registered earlier.
    app.include_router(upgrades.router, prefix=API_PREFIX)
    # Same rule, same reason: `/agents/deploy` is a fixed path under a prefix
    # whose other routes take an engine id (ADR-0019).
    app.include_router(deploy.router, prefix=API_PREFIX)
    # Also under `/agents`, and a separate module on purpose: enrollment
    # decides *whether* a host is managed, and this decides *what* is watched
    # on one that already is. They share a path prefix and nothing else.
    app.include_router(watch.router, prefix=API_PREFIX)
    # This Controller updating *itself*, which is a different question from
    # upgrading the fleet and lives under its own prefix for that reason
    # (ADR-0018).
    app.include_router(controller.router, prefix=API_PREFIX)
    # Last, and that ordering is load-bearing: Starlette matches in
    # registration order, so the dashboard's catch-all only sees paths no API
    # route claimed. See `api/web.py`.
    mount_web(app)
    return app


def create_agent_app(context: AppContext) -> FastAPI:
    """The listener agents dial, on its own port (MIGRATION section 3).

    Separate from the browser's app rather than a path on it, because the two
    have incompatible requirements at the transport: this one demands a client
    certificate and must not be terminated by anything that would strip it,
    and the other is meant to sit behind an ordinary reverse proxy. A single
    port cannot honestly be both.

    It carries **two routes and no middleware**. No CORS, because nothing here
    is reached by a browser; no lifespan, because the collector belongs to the
    process and is started once by the app that owns it. The smaller this
    surface is, the less there is to reason about on the port that faces the
    fleet.
    """
    app = FastAPI(
        title="ByStack Agent Listener",
        version=__version__,
        summary="Mutually-authenticated agent connections (ADR-0011)",
        # No schema endpoints. They document nothing an agent reads -- the
        # contract is the `.proto` -- and an unauthenticated GET that
        # enumerates the surface is not worth the convenience.
        openapi_url=None,
    )
    app.state.context = context
    app.include_router(agent_routes.router, prefix=API_PREFIX)
    return app


def create_local_agent_app(context: AppContext) -> FastAPI:
    """The unix socket the Controller's own agent dials (MIGRATION section 4).

    A third app rather than a path on either of the other two. It carries the
    single route a local agent uses and no enrollment, and it is bound to a
    socket in a directory only this user can enter -- so the reason the
    mutually-authenticated listener demands a certificate (anyone can reach
    it) simply does not apply, and the reason this one does not (only we can
    reach it) does not transfer to that one either.

    Making it an app of its own is what keeps that from being a runtime
    argument. Neither listener can serve the other's admission rule by
    accident, because neither one has the other's route.
    """
    app = FastAPI(
        title="ByStack Local Agent Listener",
        version=__version__,
        summary="The bundled agent, over a unix socket",
        openapi_url=None,
    )
    app.state.context = context
    app.include_router(agent_routes.local_router, prefix=API_PREFIX)
    return app
