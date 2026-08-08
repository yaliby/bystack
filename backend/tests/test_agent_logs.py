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
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_controller

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX
from bystack.core.graph.model import Node
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.identity import NodeKind, container_urn, host_urn
from bystack.core.ports.agent import AgentDisconnected
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.providers.agent import provider as provider_module
from bystack.providers.agent.commands import MAX_LOG_TAIL
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
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, envelope: object) -> None:
        assert isinstance(envelope, wire.Envelope)
        if self.dead:
            raise AgentDisconnected("agent is gone")
        self.sent.append(envelope)

        if envelope.WhichOneof("payload") != "logs_request" or self._answer is None:
            return
        response = self._answer(envelope.logs_request)
        if response is None:
            return
        # Delivered from a task rather than inline, because that is where a
        # real answer comes from: the receive loop, after the dispatching
        # coroutine has parked its future. Resolving inline would test an
        # ordering the transport cannot produce.
        task = asyncio.create_task(self._deliver(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, response: wire.LogsResponse) -> None:
        await asyncio.sleep(0)
        await self._provider.on_frame(wire.Envelope(logs_response=response))

    def last_request(self) -> wire.LogsRequest:
        return self.sent[-1].logs_request


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
    """`make_controller` defaults to read-only, which is the product default.
    The whole point of this route not being a command is that it answers
    anyway."""
    controller, provider = served
    assert controller.settings.read_only is True
    attached(provider, answer=answering((True, "OOMKilled")))

    with TestClient(controller.ui) as browser:
        body = browser.get(LOGS_URL, params={"urn": str(container_urn(ENGINE, C1))}).json()

    assert body["ok"] is True
    assert body["lines"] == [{"stderr": True, "text": "OOMKilled"}]
