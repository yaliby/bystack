"""Reading a container's log through its host's agent.

The feature end to end on the Controller's side: the correlation channel, the
provider's refusals, and the route. The agent half is in `agent/src/docker.rs`
and is covered by `mod log_tests` there and by `bystack.conformance`.

The cases that matter are the ones the channel exists for and that no live
agent will produce on demand -- an answer to a question we no longer remember
asking, a disconnect between the ask and the answer, and an agent too old to
know the frame at all. A happy path alone would pass against an implementation
that leaks a future on every one of them.

Nothing here touches Docker, a network or a socket.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_controller

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX
from bystack.api.routes import graph as graph_routes
from bystack.core.graph.model import Node
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import NodeKind, container_urn, host_urn
from bystack.core.ports.agent import AgentDisconnected
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.providers.agent import provider as provider_module
from bystack.providers.agent.commands import (
    DEFAULT_LOG_TAIL,
    MAX_LOG_TAIL,
    STREAM_QUEUE,
    LogLine,
    LogsEvent,
    LogsStream,
    LogsUnavailable,
)
from bystack.providers.agent.provider import AgentProvider
from bystack.runtime.writer import PartitionWriter

ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE = "AAAABBBBCCCC"
C1 = "c" * 64

LOGS_URL = f"{API_PREFIX}/graph/node/logs"


# --------------------------------------------------------------------------
# The synthetic agent
# --------------------------------------------------------------------------


class ScriptedAgent:
    """An agent that answers log reads however the test tells it to.

    ``answer`` returns the response to deliver, or ``None`` to say nothing at
    all -- which is the only way to reach the timeout path, and the reason
    this is a callback rather than a canned reply.
    """

    def __init__(
        self,
        provider: AgentProvider,
        *,
        capabilities: tuple[str, ...] = ("commands", "resync", "logs"),
        read_only: bool = False,
        answer: Callable[[wire.LogsRequest], wire.LogsResponse | None] | None = None,
        stream: Callable[[wire.LogsSubscribe], list[wire.LogsChunk]] | None = None,
    ) -> None:
        self.engine_id = ENGINE_ID
        self.read_only = read_only
        self.local = False
        self.agent_version = "0.1.0"
        self.capabilities = frozenset(capabilities)
        self.sent: list[wire.Envelope] = []
        self.dead = False
        self._provider = provider
        self._answer = answer
        self._stream = stream
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, envelope: object) -> None:
        assert isinstance(envelope, wire.Envelope)
        if self.dead:
            raise AgentDisconnected("agent is gone")
        self.sent.append(envelope)

        match envelope.WhichOneof("payload"):
            case "logs_request" if self._answer is not None:
                response = self._answer(envelope.logs_request)
                if response is not None:
                    self._spawn(self._deliver(wire.Envelope(logs_response=response)))
            case "logs_subscribe" if self._stream is not None:
                self._spawn(self._pour(self._stream(envelope.logs_subscribe)))
            case _:
                return

    def _spawn(self, work: Any) -> None:
        # Delivered from a task rather than inline, because that is where a
        # real answer comes from: the receive loop, after the dispatching
        # coroutine has parked its future. Resolving inline would test an
        # ordering the transport cannot produce.
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, envelope: wire.Envelope) -> None:
        await asyncio.sleep(0)
        await self._provider.on_frame(envelope)

    async def _pour(self, chunks: list[wire.LogsChunk]) -> None:
        """Feed a subscription its chunks, one turn of the loop apart.

        Spaced rather than dumped, because a live tail arrives that way and a
        reader that only works when everything is already queued is a reader
        that has not been tested.
        """
        for chunk in chunks:
            await self._deliver(wire.Envelope(logs_chunk=chunk))

    def last_request(self) -> wire.LogsRequest:
        return self.sent[-1].logs_request

    def subscription(self) -> wire.LogsSubscribe:
        return next(
            envelope.logs_subscribe
            for envelope in self.sent
            if envelope.WhichOneof("payload") == "logs_subscribe"
        )

    def cancelled(self) -> list[str]:
        return [
            envelope.logs_cancel.request_id
            for envelope in self.sent
            if envelope.WhichOneof("payload") == "logs_cancel"
        ]


def answering(
    *lines: tuple[bool, str], ok: bool = True, reason: str = ""
) -> Callable[[wire.LogsRequest], wire.LogsResponse]:
    def answer(request: wire.LogsRequest) -> wire.LogsResponse:
        return wire.LogsResponse(
            request_id=request.request_id,
            ok=ok,
            reason=reason,
            lines=[wire.LogLine(stderr=stderr, text=text) for stderr, text in lines],
        )

    return answer


@pytest.fixture
def wired() -> AgentProvider:
    store = InMemoryGraphStore()
    bus = InMemoryEventBus()
    return AgentProvider(ENGINE, PartitionWriter(store, bus, ENGINE))


def attached(
    provider: AgentProvider, **kwargs: Any
) -> ScriptedAgent:
    agent = ScriptedAgent(provider, **kwargs)
    provider.attach(agent)
    return agent


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_a_log_read_comes_back_with_both_streams_tagged(wired) -> None:
    """The stream tag is most of the diagnostic value: the line that explains
    a crash is almost always the one on stderr. Flattening the two into one
    blob on the way through would throw that away for a field."""
    agent = attached(wired, answer=answering((False, "listening on :80"), (True, "panic!")))

    result = await wired.logs(C1, 100)

    assert result.ok
    assert [(line.stderr, line.text) for line in result.lines] == [
        (False, "listening on :80"),
        (True, "panic!"),
    ]
    assert agent.last_request().target_id == C1
    assert agent.last_request().tail == 100


async def test_a_container_that_has_written_nothing_is_not_a_failure(wired) -> None:
    """`ok` with no lines and a refusal with no lines are different answers,
    and a client that could not tell them apart would report a healthy quiet
    container as broken."""
    attached(wired, answer=answering())

    result = await wired.logs(C1, 100)

    assert result.ok
    assert result.lines == ()
    assert result.reason is None


async def test_the_agent_is_sent_a_docker_id_and_never_a_urn(wired) -> None:
    """ADR-0009 §1: the URN scheme is Controller-side. The agent does not know
    it and must not learn it -- shipping one down the wire would be the first
    crack in that line."""
    agent = attached(wired, answer=answering())

    await wired.logs(C1, 100)

    assert agent.last_request().target_id == C1
    assert "bystack:" not in str(agent.last_request())


async def test_a_read_only_agent_still_answers_a_log_read(wired) -> None:
    """The decision this feature is shaped around. `CommandKind` is the closed
    set of *mutations* a read-only Controller refuses; refusing to show an
    operator why a container is failing because the platform is in its safe
    mode is exactly backwards."""
    attached(wired, read_only=True, answer=answering((True, "OOMKilled")))

    result = await wired.logs(C1, 100)

    assert result.ok
    assert result.lines[0].text == "OOMKilled"


async def test_an_unreasonable_tail_is_clamped_before_it_leaves(wired) -> None:
    """The agent clamps it too, and its copy is authoritative -- it holds the
    memory budget and must not trust a number we sent. This one exists so the
    request never crosses the network in the first place."""
    agent = attached(wired, answer=answering())

    await wired.logs(C1, 10_000_000)

    assert agent.last_request().tail == MAX_LOG_TAIL


# --------------------------------------------------------------------------
# What the channel exists for
# --------------------------------------------------------------------------


async def test_an_answer_to_a_forgotten_question_is_discarded_quietly(wired) -> None:
    """A late answer after a timeout arrives on the receive loop. Raising
    there would tear down a healthy connection over a cosmetic problem."""
    attached(wired, answer=answering())

    await wired.on_frame(
        wire.Envelope(logs_response=wire.LogsResponse(request_id="never-asked", ok=True))
    )

    assert wired.health().metrics["pending_logs"] == 0


async def test_a_disconnect_mid_read_answers_immediately(wired) -> None:
    """Without this the operator watches a spinner for the full deadline,
    waiting on an answer that provably cannot arrive."""
    # Answers nothing, so the read is genuinely in flight when the host goes.
    attached(wired, answer=lambda request: None)

    reading = asyncio.create_task(wired.logs(C1, 100))
    await asyncio.sleep(0)
    wired.detach("the host went away")
    result = await reading

    assert not result.ok
    assert "went away" in (result.reason or "")
    assert wired.health().metrics["pending_logs"] == 0


async def test_an_agent_that_stops_reading_times_out_with_a_reason(
    wired, monkeypatch
) -> None:
    """A silent agent is indistinguishable from a slow one, so the only
    honest answer is a deadline. Unlike a command's, this refusal carries no
    warning against retrying: a read that timed out changed nothing."""
    monkeypatch.setattr(provider_module, "LOGS_TIMEOUT", 0.01)
    attached(wired, answer=lambda request: None)

    result = await wired.logs(C1, 100)

    assert not result.ok
    assert "did not answer" in (result.reason or "")
    assert wired.health().metrics["pending_logs"] == 0


async def test_a_send_that_fails_does_not_park_a_future(wired) -> None:
    """The leak the `finally` exists to prevent: a fleet under churn that
    accumulated one future per failed dispatch would hold the caller's task
    alive until the deadline that never runs."""
    agent = attached(wired, answer=answering())
    agent.dead = True

    result = await wired.logs(C1, 100)

    assert not result.ok
    assert wired.health().metrics["pending_logs"] == 0


async def test_a_second_connection_does_not_inherit_the_first_ones_waiters(
    wired,
) -> None:
    """A reconnect gets a fresh channel. An answer arriving on the new stream
    for a read dispatched on the old one is not a late reply -- it is a reply
    to a question the previous stream is no longer around to have asked."""
    attached(wired, answer=lambda request: None)
    reading = asyncio.create_task(wired.logs(C1, 100))
    await asyncio.sleep(0)

    attached(wired, answer=answering())  # displaces the first
    result = await reading

    assert not result.ok
    assert wired.health().metrics["pending_logs"] == 0


# --------------------------------------------------------------------------
# Refusals that never reach the wire
# --------------------------------------------------------------------------


async def test_an_agent_too_old_to_know_the_frame_is_refused_by_name(wired) -> None:
    """Absence of a capability is the answer for both "too old" and "compiled
    out". Sending the frame anyway would have it ignored at the far end and
    the read would time out with no diagnosis, which is the worst available
    failure."""
    agent = attached(wired, capabilities=("commands",), answer=answering())

    result = await wired.logs(C1, 100)

    assert not result.ok
    assert "cannot read container logs" in (result.reason or "")
    assert "0.1.0" in (result.reason or "")
    # Refused before a frame crossed the network.
    assert agent.sent == []


async def test_a_host_with_no_agent_attached_is_refused_with_a_reason(wired) -> None:
    """Not an empty list. `GET /commands/actions` set the precedent: an empty
    answer with no reason renders as nothing at all, which reads as a feature
    that failed to load rather than as an answer."""
    result = await wired.logs(C1, 100)

    assert not result.ok
    assert "not currently connected" in (result.reason or "")


async def test_the_engines_own_refusal_is_carried_through(wired) -> None:
    """An operator chasing a failure should not have to guess whether the
    container or the request was wrong."""
    attached(wired, answer=answering(ok=False, reason="No such container: deadbeef"))

    result = await wired.logs(C1, 100)

    assert not result.ok
    assert result.reason == "No such container: deadbeef"


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


@pytest.fixture
def served(tmp_path):
    """A Controller with one agent-backed host holding one container.

    The graph is seeded directly rather than driven through Sync frames:
    ingest has its own file, and what is under test here is the route.
    """
    controller = make_controller(tmp_path, auto_approve=True)
    provider = controller.context.collector.agent_provider(ENGINE_ID, create=True)
    assert provider is not None
    controller.context.store.upsert(
        ENGINE,
        [
            Node(urn=host_urn(ENGINE), kind=NodeKind.HOST, name="lab-01", source=ENGINE),
            Node(
                urn=container_urn(ENGINE, C1),
                kind=NodeKind.CONTAINER,
                name="web",
                source=ENGINE,
                status="running",
            ),
        ],
    )
    return controller, provider


def test_the_route_returns_the_lines_with_their_streams(served) -> None:
    controller, provider = served
    attached(provider, answer=answering((False, "listening"), (True, "boom")))

    with TestClient(controller.ui) as browser:
        body = browser.get(
            LOGS_URL, params={"urn": str(container_urn(ENGINE, C1)), "tail": 50}
        ).json()

    assert body["ok"] is True
    assert body["target"] == str(container_urn(ENGINE, C1))
    assert body["lines"] == [
        {"stderr": False, "text": "listening"},
        {"stderr": True, "text": "boom"},
    ]


def test_a_disconnected_host_is_explained_rather_than_erroring(served) -> None:
    """200 with a reason, not a 5xx. The container is on screen and the
    operator clicked it; "this host is offline" is an answer, and an error
    page over a node they can plainly see is not."""
    controller, _ = served

    with TestClient(controller.ui) as browser:
        response = browser.get(LOGS_URL, params={"urn": str(container_urn(ENGINE, C1))})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "not currently connected" in response.json()["reason"]


def test_a_malformed_urn_is_a_bad_request(served) -> None:
    controller, _ = served

    with TestClient(controller.ui) as browser:
        response = browser.get(LOGS_URL, params={"urn": "not-a-urn"})

    assert response.status_code == 400


def test_a_urn_that_is_not_a_container_is_a_bad_request(served) -> None:
    """Only containers have logs. A host URN here is a client bug, and
    answering it with an empty list would hide one."""
    controller, _ = served

    with TestClient(controller.ui) as browser:
        response = browser.get(LOGS_URL, params={"urn": str(host_urn(ENGINE))})

    assert response.status_code == 400


def test_a_container_that_is_not_in_the_graph_is_not_found(served) -> None:
    controller, _ = served

    with TestClient(controller.ui) as browser:
        response = browser.get(LOGS_URL, params={"urn": str(container_urn(ENGINE, "d" * 64))})

    assert response.status_code == 404


def test_a_host_with_no_agent_provider_at_all_is_not_found(served) -> None:
    """Distinct from a disconnected agent, which is answered with a reason:
    this one cannot become true by waiting."""
    controller, _ = served
    other = container_urn("BBBBCCCCDDDD", C1)
    controller.context.store.upsert(
        "BBBBCCCCDDDD",
        [
            Node(
                urn=other,
                kind=NodeKind.CONTAINER,
                name="web",
                source="BBBBCCCCDDDD",
                status="running",
            )
        ],
    )

    with TestClient(controller.ui) as browser:
        response = browser.get(LOGS_URL, params={"urn": str(other)})

    assert response.status_code == 404


def test_the_tail_is_bounded_by_the_schema(served) -> None:
    """Refused by validation rather than clamped silently: a client asking for
    a million lines has misunderstood the endpoint, and quietly giving it two
    thousand would let that misunderstanding ship."""
    controller, provider = served
    attached(provider, answer=answering())

    with TestClient(controller.ui) as browser:
        response = browser.get(
            LOGS_URL, params={"urn": str(container_urn(ENGINE, C1)), "tail": 10_000_000}
        )

    assert response.status_code == 422


def test_a_read_only_controller_still_serves_logs(served) -> None:
    """`make_controller` defaults to read-only -- deliberately not the product
    default any more (ADR-0014 flipped that), which is the point: this fixture
    pins the mode rather than inheriting it. The whole reason this route is not
    a command is that it answers in that mode anyway."""
    controller, provider = served
    assert controller.settings.read_only is True
    attached(provider, answer=answering((True, "OOMKilled")))

    with TestClient(controller.ui) as browser:
        body = browser.get(LOGS_URL, params={"urn": str(container_urn(ENGINE, C1))}).json()

    assert body["ok"] is True
    assert body["lines"] == [{"stderr": True, "text": "OOMKilled"}]


# --------------------------------------------------------------------------
# The live tail
# --------------------------------------------------------------------------
#
# Everything above is the one-shot read. This is the streaming sibling, which
# landed later and brought three things the one-shot path does not have: a
# bounded queue with a drop policy, a subscription registry with a lifetime
# longer than one request, and an SSE encoder. Each is tested here at the
# lowest level it can be, because each fails in a way the happy path cannot
# see -- a queue that drops the terminal event hangs a browser forever, and
# nothing about a working tail reveals it.

STREAM_URL = f"{API_PREFIX}/graph/node/logs/stream"

#: What an agent that can do everything advertises. The one-shot tests keep
#: the shorter tuple on purpose -- `logs` without `logs-stream` is a real
#: agent, the one that predates streaming, and it has its own check below.
STREAMING = ("commands", "resync", "logs", "logs-stream")


def chunk(
    request_id: str,
    *lines: tuple[bool, str],
    done: bool = False,
    reason: str = "",
) -> wire.LogsChunk:
    return wire.LogsChunk(
        request_id=request_id,
        lines=[wire.LogLine(stderr=stderr, text=text) for stderr, text in lines],
        done=done,
        reason=reason,
    )


# -- the queue -------------------------------------------------------------


async def test_a_reader_that_keeps_up_is_handed_every_line() -> None:
    stream = LogsStream("req", C1)

    stream.offer(LogsEvent(lines=(LogLine(False, "one"),)))
    stream.offer(LogsEvent(lines=(LogLine(True, "two"),)))

    assert (await stream.next()).lines == (LogLine(False, "one"),)
    assert (await stream.next()).lines == (LogLine(True, "two"),)


async def test_a_reader_that_falls_behind_loses_the_oldest_and_is_told() -> None:
    """The drop policy, which is the whole backpressure decision in one place.

    Oldest-first is the right loss for a live tail -- an operator watching
    output scroll wants the newest lines, and a tail that stalled to preserve
    history nobody asked for would be the wrong feature. What must not happen
    is that it is silent: a gap the UI cannot mark is a log read as
    continuous, and two adjacent lines that were never adjacent are a wrong
    conclusion about a crash.
    """
    stream = LogsStream("req", C1)
    for index in range(STREAM_QUEUE + 10):
        stream.offer(LogsEvent(lines=(LogLine(False, f"line-{index}"),)))

    drained = [await stream.next() for _ in range(STREAM_QUEUE)]

    # The oldest went first: what survives is the tail, not the head.
    assert drained[0].lines == (LogLine(False, "line-10"),)
    assert drained[-1].lines == (LogLine(False, f"line-{STREAM_QUEUE + 9}"),)
    # Ten went, and every one of them is accounted for. The count rides out on
    # the next event *offered* rather than the next one read -- it is attached
    # where the loss happened, so it cannot be lost by a reader that stops.
    assert sum(event.dropped for event in drained) == 10


async def test_the_end_of_a_stream_is_never_the_event_that_gets_dropped() -> None:
    """A reader that missed the terminal event waits forever for a stream that
    has already ended. Every other event is expendable; this one is not."""
    stream = LogsStream("req", C1)
    for index in range(STREAM_QUEUE + 5):
        stream.offer(LogsEvent(lines=(LogLine(False, f"line-{index}"),)))

    stream.close("the host went away")

    events = [await stream.next() for _ in range(STREAM_QUEUE)]
    assert events[-1].done is True
    assert events[-1].reason == "the host went away"


async def test_a_stream_that_lost_lines_says_so_on_the_way_out() -> None:
    """The count rides out on the terminal event too. Otherwise a tail that
    ended having dropped its last lines reports the last thing the operator
    saw as the last thing the container wrote."""
    stream = LogsStream("req", C1)
    for index in range(STREAM_QUEUE + 3):
        stream.offer(LogsEvent(lines=(LogLine(False, f"line-{index}"),)))

    # Closing against a full queue costs a fourth line to make room for the
    # terminal event itself, which is the trade `offer` makes deliberately.
    stream.close("ended")

    drained = []
    while not drained or not drained[-1].done:
        drained.append(await stream.next())

    assert drained[-1].reason == "ended"
    assert sum(event.dropped for event in drained) == 4


async def test_closing_twice_does_not_end_a_stream_twice() -> None:
    """`close` is idempotent because both the disconnect path and the route's
    `finally` can reach it, and a second terminal event would be read as a
    second stream ending."""
    stream = LogsStream("req", C1)
    stream.close("first")
    stream.close("second")

    assert (await stream.next()).reason == "first"
    # And nothing behind it: a second terminal event would be read as a second
    # stream ending, which is a reconnect the operator never asked for.
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await stream.next()


# -- the subscription registry ---------------------------------------------


async def test_a_live_tail_carries_its_lines_through_with_their_tags(wired) -> None:
    agent = attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 100) as stream:
        request_id = agent.subscription().request_id
        await wired.on_frame(
            wire.Envelope(logs_chunk=chunk(request_id, (False, "up"), (True, "boom")))
        )

        event = await stream.next()

    assert event.lines == (LogLine(False, "up"), LogLine(True, "boom"))


async def test_the_agent_is_sent_a_docker_id_and_never_a_urn_on_a_tail(wired) -> None:
    """The same line ADR-0009 §1 draws for commands and for the one-shot read.
    The agent does not know the URN scheme and must not learn it."""
    agent = attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 100):
        assert agent.subscription().target_id == C1


async def test_an_unreasonable_backfill_is_clamped_before_it_leaves(wired) -> None:
    agent = attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 10_000_000):
        assert agent.subscription().tail == MAX_LOG_TAIL


async def test_a_disconnect_ends_every_live_tail_with_a_reason(wired) -> None:
    """The property that makes `LogsSubscriptions` worth being a class. A
    browser watching a log when the host goes away must be told, not left on a
    stream that silently stops -- which is indistinguishable from a container
    that went quiet."""
    attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 100) as stream:
        wired.detach("the agent went away")

        event = await stream.next()

    assert event.done is True
    assert event.reason == "the agent went away"


async def test_a_chunk_for_a_subscription_that_has_gone_is_ignored(wired) -> None:
    """An in-flight chunk arriving after a cancel is ordinary, not an error.
    Raising here would tear down a healthy connection -- and with it every
    other host's topology -- over a race that resolves itself."""
    attached(wired, capabilities=STREAMING)

    await wired.on_frame(wire.Envelope(logs_chunk=chunk("never-asked", (False, "hi"))))

    assert wired.health().metrics["live_log_streams"] == 0


async def test_an_ended_tail_is_forgotten(wired) -> None:
    """Otherwise a Controller managing a host with a lot of short-lived
    containers accumulates one dead subscription per log anyone ever opened."""
    agent = attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 100) as stream:
        request_id = agent.subscription().request_id
        assert wired.health().metrics["live_log_streams"] == 1

        await wired.on_frame(wire.Envelope(logs_chunk=chunk(request_id, done=True)))
        assert (await stream.next()).done is True

    assert wired.health().metrics["live_log_streams"] == 0


async def test_leaving_the_tail_cancels_it_on_the_agent(wired) -> None:
    """The reason `follow` is a context manager. An uncancelled subscription is
    a `docker logs --follow` running on someone's machine, pushing bytes down
    an uplink nobody is reading, until the agent disconnects."""
    agent = attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 100):
        request_id = agent.subscription().request_id

    assert agent.cancelled() == [request_id]
    assert wired.health().metrics["live_log_streams"] == 0


async def test_an_exception_in_the_reader_still_cancels_the_tail(wired) -> None:
    """`finally`, not a trailing statement: the route's generator is closed by
    Starlette when the browser disconnects, and that arrives as an exception."""
    agent = attached(wired, capabilities=STREAMING)
    with pytest.raises(RuntimeError):
        async with wired.follow(C1, 100):
            raise RuntimeError("the browser went away")

    assert len(agent.cancelled()) == 1


async def test_a_second_connection_does_not_inherit_the_first_ones_tails(wired) -> None:
    """`attach` builds a fresh registry, so a chunk arriving on a new
    connection cannot be routed to a reader from the old one."""
    first = attached(wired, capabilities=STREAMING)
    async with wired.follow(C1, 100) as stream:
        request_id = first.subscription().request_id
        attached(wired, capabilities=STREAMING)

        # Displacing the first session ended its tails.
        assert (await stream.next()).done is True
        assert wired.health().metrics["live_log_streams"] == 0

        # And the old id routes nowhere rather than into the new registry.
        await wired.on_frame(wire.Envelope(logs_chunk=chunk(request_id, (False, "late"))))
        assert wired.health().metrics["live_log_streams"] == 0


# -- refusals, all before the response begins ------------------------------


async def test_an_agent_too_old_to_stream_is_refused_by_name(wired) -> None:
    """`logs-stream` is deliberately not implied by `logs`: an agent that
    predates streaming answers one-shot reads perfectly well, and the caller
    needs to know which of the two it is holding."""
    agent = attached(wired, capabilities=("commands", "logs"))

    with pytest.raises(LogsUnavailable, match="cannot stream"):
        async with wired.follow(C1, 100):
            pass

    assert "0.1.0" in str(agent.agent_version)
    # Refused before a frame crossed the network.
    assert agent.sent == []


async def test_a_host_with_no_agent_attached_cannot_be_followed(wired) -> None:
    with pytest.raises(LogsUnavailable, match="not currently connected"):
        async with wired.follow(C1, 100):
            pass


async def test_a_disconnect_racing_the_subscribe_is_a_refusal_not_a_crash(wired) -> None:
    """The window between the liveness check and the send is small and real,
    and it is the likeliest moment for it to close: the host an operator opens
    a log panel on is often the host that just went away.

    It must leave by the same door as every other refusal. An `AgentDisconnected`
    escaping `__aenter__` is an unhandled error and a 500 over a container the
    operator can plainly see; `LogsUnavailable` is the 409 the route knows how
    to say.
    """
    agent = attached(wired, capabilities=STREAMING)
    agent.dead = True

    with pytest.raises(LogsUnavailable, match="disconnected"):
        async with wired.follow(C1, 100):
            pass

    # And it left nothing parked behind it.
    assert wired.health().metrics["live_log_streams"] == 0


# -- the route -------------------------------------------------------------


def pouring(
    *lines: tuple[bool, str], end: str | None = ""
) -> Callable[[wire.LogsSubscribe], list[wire.LogsChunk]]:
    """An agent that writes these lines and then, unless told otherwise, stops.

    ``end=None`` leaves the tail open, which is the only way to reach the idle
    path -- a stream that ends promptly can never be seen to go quiet.
    """

    def build(request: wire.LogsSubscribe) -> list[wire.LogsChunk]:
        chunks = [chunk(request.request_id, line) for line in lines]
        if end is not None:
            chunks.append(chunk(request.request_id, done=True, reason=end))
        return chunks

    return build


def read_sse(response: Any, *, until: str) -> list[tuple[str, str]]:
    """Parse events off a live response, stopping at the first ``until``.

    Stops rather than draining, because a live tail has no end to drain to and
    the test would otherwise be the hang it is checking against. Comments are
    skipped the way a real client skips them -- none of these tails idles long
    enough to see one, but a parser that choked on a keepalive would be
    describing a client nobody has.
    """
    events: list[tuple[str, str]] = []
    name = ""
    for raw in response.iter_lines():
        if raw.startswith("event: "):
            name = raw.removeprefix("event: ")
        elif raw.startswith("data: "):
            events.append((name, raw.removeprefix("data: ")))
            if name == until:
                break
    return events


def test_the_stream_route_delivers_lines_and_then_says_it_ended(served) -> None:
    controller, provider = served
    attached(
        provider,
        capabilities=STREAMING,
        stream=pouring((False, "listening on :80"), (True, "panic!")),
    )

    with TestClient(controller.ui) as browser, browser.stream(
        "GET", STREAM_URL, params={"urn": str(container_urn(ENGINE, C1))}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = read_sse(response, until="end")

    assert [(name, json.loads(data)) for name, data in events] == [
        ("lines", {"lines": [{"stderr": False, "text": "listening on :80"}], "dropped": 0}),
        ("lines", {"lines": [{"stderr": True, "text": "panic!"}], "dropped": 0}),
        ("end", {"reason": None}),
    ]


def test_an_ordinary_ending_and_a_failure_are_told_apart(served) -> None:
    """Silence after a crash and silence after a clean stop look identical
    otherwise, and an operator will read the first as the second."""
    controller, provider = served
    attached(provider, capabilities=STREAMING, stream=pouring(end="the daemon hung up"))

    with TestClient(controller.ui) as browser, browser.stream(
        "GET", STREAM_URL, params={"urn": str(container_urn(ENGINE, C1))}
    ) as response:
        events = read_sse(response, until="end")

    assert json.loads(events[-1][1]) == {"reason": "the daemon hung up"}


def test_a_log_line_holding_a_newline_does_not_break_the_event(served) -> None:
    """A container writes whatever it likes, and a raw newline inside an SSE
    `data:` field ends the field -- so an unescaped multi-line log line would
    be silently truncated at best and would desynchronise the parser at worst.
    `json.dumps` makes that impossible by construction rather than by
    remembering to escape."""
    controller, provider = served
    hostile = "Traceback:\n  File x\n\nevent: end\ndata: {}\n\n"
    attached(provider, capabilities=STREAMING, stream=pouring((True, hostile)))

    with TestClient(controller.ui) as browser, browser.stream(
        "GET", STREAM_URL, params={"urn": str(container_urn(ENGINE, C1))}
    ) as response:
        events = read_sse(response, until="end")

    # One `lines` event and one `end`: the injected `event: end` travelled as
    # data and did not become a frame of its own.
    assert [name for name, _ in events] == ["lines", "end"]
    assert json.loads(events[0][1])["lines"] == [{"stderr": True, "text": hostile}]


async def test_an_idle_stream_is_kept_alive(served, monkeypatch) -> None:
    """A quiet container is the normal case, not an edge one. Without a
    heartbeat the connection carries no bytes at all between lines, and the
    reverse proxy this listener was kept able to sit behind closes it on its
    own read timeout -- after which `EventSource` reconnects, re-subscribes,
    and replays the whole backfill. The operator watching an idle log sees the
    same screenful appear again every minute, and the managed host pays for a
    new `docker logs --follow` each time.

    Driven through the response's own iterator rather than `TestClient`,
    because this is the one tail with no end: the client would sit in its
    portal at teardown waiting for a generator that is deliberately never
    going to finish, which is the hang this test would then *be*.
    """
    controller, provider = served
    monkeypatch.setattr(graph_routes, "STREAM_KEEPALIVE", 0.05)
    # Never ends and never writes: the tail is open on a container with
    # nothing to say, which is what most containers are doing most of the time.
    agent = attached(provider, capabilities=STREAMING, stream=pouring(end=None))

    response = await graph_routes.stream_node_logs(
        controller.context.store,
        controller.context.collector,
        urn=str(container_urn(ENGINE, C1)),
        tail=DEFAULT_LOG_TAIL,
    )
    body = response.body_iterator
    try:
        # Bounded, because the failure this guards against is silence: without
        # the heartbeat `anext` never returns at all, and an unbounded wait
        # would make this check hang CI rather than fail it.
        async with asyncio.timeout(2.0):
            assert await anext(body) == ": keepalive\n\n"
            assert await anext(body) == ": keepalive\n\n"
    except TimeoutError:
        raise AssertionError("an idle stream sent nothing; a proxy would close it") from None
    finally:
        # What Starlette does when the browser goes away.
        await body.aclose()

    # And going away is what cancels the follow on the managed host, rather
    # than leaving `docker logs --follow` running for nobody.
    assert len(agent.cancelled()) == 1
    assert provider.health().metrics["live_log_streams"] == 0


def test_an_agent_too_old_to_stream_is_refused_before_the_response_begins(served) -> None:
    """A status code, not a 200 whose first event is an apology. Once
    `StreamingResponse` has begun the headers are gone and the only way left to
    say no is an event nobody may be reading yet."""
    controller, provider = served
    attached(provider, capabilities=("commands", "logs"))

    with TestClient(controller.ui) as browser:
        response = browser.get(STREAM_URL, params={"urn": str(container_urn(ENGINE, C1))})

    assert response.status_code == 409
    assert "cannot stream" in response.json()["detail"]


def test_a_disconnected_host_cannot_be_followed(served) -> None:
    """Deliberately unlike the one-shot read, which answers 200 with a reason
    in the body. There is no body to put a reason in until the stream starts,
    and starting one that immediately apologises is worse than refusing."""
    controller, _ = served

    with TestClient(controller.ui) as browser:
        response = browser.get(STREAM_URL, params={"urn": str(container_urn(ENGINE, C1))})

    assert response.status_code == 409
    assert "not currently connected" in response.json()["detail"]


def test_the_stream_route_checks_the_urn_the_same_way(served) -> None:
    controller, _ = served

    with TestClient(controller.ui) as browser:
        assert browser.get(STREAM_URL, params={"urn": "not-a-urn"}).status_code == 400
        assert browser.get(
            STREAM_URL, params={"urn": str(host_urn(ENGINE))}
        ).status_code == 400
        assert browser.get(
            STREAM_URL, params={"urn": str(container_urn(ENGINE, "d" * 64))}
        ).status_code == 404


def test_a_read_only_controller_still_streams_logs(served) -> None:
    """The same line the one-shot read draws. Read-only is the closed set of
    mutations refused; looking is always allowed."""
    controller, provider = served
    assert controller.settings.read_only is True
    attached(provider, capabilities=STREAMING, stream=pouring((True, "OOMKilled")))

    with TestClient(controller.ui) as browser, browser.stream(
        "GET", STREAM_URL, params={"urn": str(container_urn(ENGINE, C1))}
    ) as response:
        events = read_sse(response, until="end")

    assert json.loads(events[0][1])["lines"] == [{"stderr": True, "text": "OOMKilled"}]
