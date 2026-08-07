"""Event bus port.

Every infrastructure change produces an event; the UI reacts to events. The
bus is the seam between the collector (producer) and the API layer
(consumer), and it is where the platform's backpressure policy lives.

**Backpressure is a memory-budget decision, not a nicety.** Per-subscriber
queues are the classic silent RAM killer in fan-out systems: one slow
WebSocket client on a bad connection can grow a buffer without bound while
the process quietly walks past its 512MB budget. The policy here is
deliberate and absolute:

    A subscriber that cannot keep up loses its backlog and is marked
    ``lagging``. It never gets a bigger buffer.

A lagging subscriber recovers by re-requesting a snapshot -- which is cheap,
correct, and bounded -- rather than by replaying a backlog we would have had
to store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Topic(StrEnum):
    """The bus's topics. Deliberately few."""

    GRAPH_DELTA = "graph.delta"
    """:class:`~bystack.core.graph.delta.GraphDelta` -- incremental changes."""

    LIFECYCLE = "lifecycle"
    """Provider/transport state changes. Operational, not infrastructural."""


class Lagged(Exception):
    """Raised into a subscriber's stream when its backlog was dropped.

    Recovery is always the same: re-read a snapshot and resume from its
    sequence number. Never attempt to reconstruct the missed messages.
    """

    def __init__(self, dropped: int) -> None:
        super().__init__(f"subscriber fell behind; {dropped} message(s) dropped")
        self.dropped = dropped


@runtime_checkable
class Subscription(Protocol):
    """A message stream that can be released deterministically.

    Plain :class:`~collections.abc.AsyncIterator` is not enough here. Every
    consumer closes its stream on the way out -- that is what returns the
    mailbox to the bus instead of waiting for the generator to be collected --
    so ``aclose`` belongs in the contract rather than in whatever the current
    implementation happens to return.
    """

    def __aiter__(self) -> AsyncIterator[object]: ...

    def __anext__(self) -> Awaitable[object]: ...

    async def aclose(self) -> None:
        """Drop the subscription and release its mailbox."""
        ...


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, topic: Topic, message: object) -> None:
        """Fan out to current subscribers.

        Never blocks on a slow subscriber and never fails because of one --
        publishing is on the collector's hot path, and a stalled UI client
        must not be able to stall discovery.
        """
        ...

    def subscribe(self, topic: Topic, *, maxsize: int = 256) -> Subscription:
        """Stream messages. Raises :class:`Lagged` if the backlog overflows."""
        ...

    async def aclose(self) -> None:
        """Shut the bus down: stop publishing and drop every subscription.

        Called from the API's lifespan teardown, so it is part of the port
        rather than an implementation detail of the in-process bus -- a
        replacement implementation that omitted it would typecheck and then
        fail at shutdown.
        """
        ...
