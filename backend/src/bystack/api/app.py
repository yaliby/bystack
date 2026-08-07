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
from bystack.api.routes import graph, health, stream
from bystack.config import Settings
from bystack.core.graph.store import InMemoryGraphStore
from bystack.infra.audit.memory import InMemoryAuditLog
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.runtime.collector import Collector
from bystack.runtime.commands import CommandService

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
        InMemoryAuditLog(),
        # A lookup closure rather than the collector itself: the command
        # service needs to resolve one provider by partition key and nothing
        # else, and handing it the supervisor would let it grow a dependency
        # on provider lifecycle that it has no business having.
        lambda source: collector.providers.get(source),
        read_only=settings.read_only,
    )
    return AppContext(
        settings=settings, store=store, bus=bus, collector=collector, commands=commands
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.default()
    context = build_context(settings)

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
    # Agents share the browser's port for now. MIGRATION §3 puts them on a
    # separate listener, because one side is browser-facing and may sit behind
    # an ordinary reverse proxy while the other requires client certificates —
    # and that split only becomes meaningful once ADR-0011's mTLS exists to be
    # required.
    app.include_router(agent_routes.router, prefix=API_PREFIX)
    return app
