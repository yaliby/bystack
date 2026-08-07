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
from bystack.runtime.collector import Collector
from bystack.runtime.commands import CommandService


@dataclass(slots=True)
class AppContext:
    """Everything the API layer is allowed to reach."""

    settings: Settings
    store: GraphStore
    bus: EventBus
    collector: Collector
    commands: CommandService


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


Context = Annotated[AppContext, Depends(get_context)]
Store = Annotated[GraphStore, Depends(get_store)]
Bus = Annotated[EventBus, Depends(get_bus)]
Collectors = Annotated[Collector, Depends(get_collector)]
Commands = Annotated[CommandService, Depends(get_commands)]
