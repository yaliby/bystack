"""In-process event bus.

Sufficient for the single-instance deployment target. When the collector is
split into its own process, this class is replaced by a Redis/NATS-backed
implementation of the same port and nothing above it changes.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator
from typing import Final

from bystack.core.ports.eventbus import Lagged, Topic

#: Sentinel pushed into a subscriber's queue after its backlog was dropped.
_LAGGED: Final = object()


class _Subscription:
    """One subscriber's bounded mailbox."""

    __slots__ = ("queue", "dropped")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, message: object) -> None:
        """Non-blocking delivery. Never grows the mailbox."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop the entire backlog rather than the newest message. A
            # subscriber this far behind cannot use stale deltas anyway --
            # its only correct recovery is a fresh snapshot, so partial
            # history would be dead weight against the memory budget.
            drained = 0
            while True:
                try:
                    self.queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            self.dropped += drained
            self.queue.put_nowait(_LAGGED)


class InMemoryEventBus:
    """Fan-out bus with drop-on-overflow backpressure."""

    __slots__ = ("_subscriptions", "_closed")

    def __init__(self) -> None:
        self._subscriptions: dict[Topic, set[_Subscription]] = defaultdict(set)
        self._closed = False

    async def publish(self, topic: Topic, message: object) -> None:
        if self._closed:
            return
        # Snapshot the set: a subscriber may unsubscribe while we iterate.
        for subscription in tuple(self._subscriptions.get(topic, ())):
            subscription.offer(message)

    def subscribe(self, topic: Topic, *, maxsize: int = 256) -> AsyncGenerator[object, None]:
        subscription = _Subscription(maxsize)
        self._subscriptions[topic].add(subscription)
        return self._stream(topic, subscription)

    async def _stream(
        self, topic: Topic, subscription: _Subscription
    ) -> AsyncGenerator[object, None]:
        try:
            while True:
                message = await subscription.queue.get()
                if message is _LAGGED:
                    dropped, subscription.dropped = subscription.dropped, 0
                    raise Lagged(dropped)
                yield message
        finally:
            # Runs on normal exit, on Lagged, and on task cancellation, so a
            # disconnected WebSocket can never leave a mailbox filling up
            # behind it.
            self._subscriptions[topic].discard(subscription)

    def subscriber_count(self, topic: Topic) -> int:
        return len(self._subscriptions.get(topic, ()))

    async def aclose(self) -> None:
        self._closed = True
        self._subscriptions.clear()
