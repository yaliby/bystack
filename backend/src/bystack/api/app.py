"""Composition root.

The only module permitted to know about every layer at once. Everything else
receives what it needs through a constructor or a dependency, which is what
keeps the dependency graph acyclic and every module independently testable.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bystack.api.deps import AppContext
from bystack.api.routes import agents as agent_routes
from bystack.api.routes import commands as commands_routes
from bystack.api.routes import enrollment, graph, health, stream
from bystack.config import Settings
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.ports.command import AuditLog
from bystack.infra.audit.durable import DurableAuditLog
from bystack.infra.audit.memory import InMemoryAuditLog
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.runtime.collector import Collector
from bystack.runtime.commands import CommandService
from bystack.runtime.localagent import LocalAgent
from bystack.runtime.trust import AgentTrust

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def build_context(settings: Settings) -> AppContext:
    """Wire the object graph.

    Swapping any of these for another implementation of the same port -- a
    Redis bus, an RPC-backed store when the collector moves to its own
    process -- is a change to this function and nowhere else.
    """
    store = InMemoryGraphStore()
    bus = InMemoryEventBus()
    collector = Collector.from_settings(settings, store, bus)
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
        trust=AgentTrust.from_settings(settings),
        # Constructed whether or not it can run, so that a Controller which
        # could not spawn one can say why rather than showing an empty fleet.
        local_agent=LocalAgent(settings),
    )


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
        try:
            yield
        finally:
            await context.collector.stop()
            await context.bus.aclose()

    app = FastAPI(
        title="ByStack Control Plane",
        version="0.1.0",
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
        version="0.1.0",
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
        version="0.1.0",
        summary="The bundled agent, over a unix socket",
        openapi_url=None,
    )
    app.state.context = context
    app.include_router(agent_routes.local_router, prefix=API_PREFIX)
    return app
