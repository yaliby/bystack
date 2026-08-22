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
from bystack.runtime.deploy import DeployService
from bystack.runtime.localagent import LocalAgent
from bystack.runtime.selfupdate import SelfUpdateService
from bystack.runtime.trust import AgentTrust
from bystack.runtime.upgrade import UpgradeService


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

    upgrades: UpgradeService
    """Signed agent releases, and the staged rollout that distributes them.

    Reached from the API because starting a rollout and reading its progress
    are both operator actions on the browser's side of the split. It is not
    reached from the agent listener at all: an agent never asks to be upgraded,
    it is offered a release over the stream it already holds and decides for
    itself (ADR-0017).
    """

    selfupdate: SelfUpdateService
    """This Controller's own updater, and the fleet cascade behind it.

    Beside `upgrades` rather than inside it, and the two are kept apart on
    purpose. `upgrades` distributes to hosts that verify for themselves; this
    asks a root process on *this* machine to replace the file this process is
    running out of. They share the signature contract and nothing else --
    different trust boundary, different failure mode, different thing to be
    careful about (ADR-0018).
    """

    deploy: DeployService
    """Installing an agent on a machine that does not have one (ADR-0019).

    The only thing in this context that ever holds a credential, and it holds
    one for the length of one run and writes it nowhere. Beside `upgrades`
    rather than inside it for the reason `selfupdate` is: that service
    distributes to hosts that already trust us and verify for themselves, and
    this one opens an outbound connection to a machine that has never heard of
    this Controller. Same fleet, opposite trust boundary.

    Reached from the API only. Nothing on the agent listener can start a
    deployment, which is the same rule enrollment follows.
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


def get_upgrades(context: Annotated[AppContext, Depends(get_context)]) -> UpgradeService:
    return context.upgrades


def get_selfupdate(context: Annotated[AppContext, Depends(get_context)]) -> SelfUpdateService:
    return context.selfupdate


def get_deploy(context: Annotated[AppContext, Depends(get_context)]) -> DeployService:
    return context.deploy


Context = Annotated[AppContext, Depends(get_context)]
Store = Annotated[GraphStore, Depends(get_store)]
Bus = Annotated[EventBus, Depends(get_bus)]
Collectors = Annotated[Collector, Depends(get_collector)]
Commands = Annotated[CommandService, Depends(get_commands)]
Trust = Annotated[AgentTrust, Depends(get_trust)]
Upgrades = Annotated[UpgradeService, Depends(get_upgrades)]
SelfUpdate = Annotated[SelfUpdateService, Depends(get_selfupdate)]
Deploy = Annotated[DeployService, Depends(get_deploy)]
