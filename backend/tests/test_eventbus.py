"""Event bus fan-out and the backpressure policy.

These tests exist because the policy they encode is a memory-budget
requirement: an unbounded per-subscriber queue is how a fan-out system
quietly walks past 512MB while everything looks healthy.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from bystack.core.ports.eventbus import Lagged, Topic
from bystack.infra.eventbus.memory import InMemoryEventBus


async def test_message_reaches_every_subscriber() -> None:
    bus = InMemoryEventBus()
    async with aclosing(bus.subscribe(Topic.GRAPH_DELTA)) as first, aclosing(
        bus.subscribe(Topic.GRAPH_DELTA)
    ) as second:
        await bus.publish(Topic.GRAPH_DELTA, "x")

        assert await anext(first) == "x"
        assert await anext(second) == "x"


async def test_topics_are_isolated() -> None:
    bus = InMemoryEventBus()
    async with aclosing(bus.subscribe(Topic.GRAPH_DELTA)) as stream:
        await bus.publish(Topic.LIFECYCLE, "not-for-you")
        await bus.publish(Topic.GRAPH_DELTA, "mine")

        assert await anext(stream) == "mine"


async def test_slow_subscriber_loses_its_backlog_instead_of_growing_it() -> None:
    bus = InMemoryEventBus()
    async with aclosing(bus.subscribe(Topic.GRAPH_DELTA, maxsize=2)) as stream:
        for i in range(10):
            await bus.publish(Topic.GRAPH_DELTA, i)

        with pytest.raises(Lagged) as caught:
            await anext(stream)

        assert caught.value.dropped > 0


async def test_a_slow_subscriber_cannot_stall_the_publisher() -> None:
    # Publishing is on the collector's hot path. A stalled UI client must
    # never be able to stall infrastructure discovery.
    bus = InMemoryEventBus()
    async with aclosing(bus.subscribe(Topic.GRAPH_DELTA, maxsize=1)):
        await asyncio.wait_for(
            asyncio.gather(*(bus.publish(Topic.GRAPH_DELTA, i) for i in range(1000))),
            timeout=2.0,
        )


async def test_subscriber_is_released_when_its_stream_closes() -> None:
    # A disconnected WebSocket must not leave a mailbox filling up behind it.
    bus = InMemoryEventBus()
    stream = bus.subscribe(Topic.GRAPH_DELTA)
    await bus.publish(Topic.GRAPH_DELTA, "x")
    await anext(stream)
    assert bus.subscriber_count(Topic.GRAPH_DELTA) == 1

    await stream.aclose()

    assert bus.subscriber_count(Topic.GRAPH_DELTA) == 0


async def test_publishing_with_no_subscribers_is_harmless() -> None:
    bus = InMemoryEventBus()
    await bus.publish(Topic.GRAPH_DELTA, "x")
