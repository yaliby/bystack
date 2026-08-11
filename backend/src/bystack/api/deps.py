"""Dependency injection wiring.

Components are constructed once in the composition root and stashed on
``app.state``; routes receive them through ``Depends``. Nothing imports a
singleton, so every route is testable against a hand-built store with no
Docker daemon, no event loop tricks and no monkeypatching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from bystack.config import Settings
from bystack.core.graph.store import GraphStore
from bystack.core.ports.eventbus import EventBus
from bystack.core.ports.watch import WatchStore
from bystack.runtime.collector import Collector
from bystack.runtime.commands import CommandService
from bystack.runtime.localagent import LocalAgent
from bystack.runtime.trust import AgentTrust


@dataclass(slots=True)
class AppContext:
    """Everything the API layer is allowed to reach."""

    settings: Settings
    store: GraphStore
    bus: EventBus
    collector: Collector
    commands: CommandService
    trust: AgentTrust
    """Enrollment, admission and renewal (ADR-0011).

    Shared by both listeners, and it has to be: the operator mints a token on
    the browser port and the agent redeems it on the other one. One CA, one
    registry, one answer to "is this host allowed".
    """

    watchlist: WatchStore
    """Which units and processes each host was asked to watch.

    Configuration rather than discovery, and the only thing in this context
    that cannot be rebuilt from the fleet: nothing out there knows what the
    operator selected (ADR-0001's first durable category). Reached from the
    API because editing it *is* the feature, and from the agent listener
    because a connection is where a host is told.
    """

    local_agent: LocalAgent
    """The agent this Controller spawned for its own machine.

    Reachable from the API only so that a first run can be *explained*: if it
    could not start, the reason is the difference between a graph that is empty
    because this machine runs nothing and one that is empty because the binary
    is missing. Nothing routes commands or observations through it -- those go
    through the provider its child creates, like every other host's.
    """


def get_context(request: Request) -> AppContext:
    return request.app.state.context  # type: ignore[no-any-return]


def get_store(context: Annotated[AppContext, Depends(get_context)]) -> GraphStore:
    return context.store


def get_bus(context: Annotated[AppContext, Depends(get_context)]) -> EventBus:
    return context.bus


def get_collector(context: Annotated[AppContext, Depends(get_context)]) -> Collector:
    return context.collector


def get_commands(context: Annotated[AppContext, Depends(get_context)]) -> CommandService:
    return context.commands


def get_trust(context: Annotated[AppContext, Depends(get_context)]) -> AgentTrust:
    return context.trust


Context = Annotated[AppContext, Depends(get_context)]
Store = Annotated[GraphStore, Depends(get_store)]
Bus = Annotated[EventBus, Depends(get_bus)]
Collectors = Annotated[Collector, Depends(get_collector)]
Commands = Annotated[CommandService, Depends(get_commands)]
Trust = Annotated[AgentTrust, Depends(get_trust)]
