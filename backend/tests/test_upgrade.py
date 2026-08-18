"""Distributing a signed agent release (ADR-0017).

The Controller's half of the feature: what it will hand out, what it refuses
to hand out, and the staged rollout that hands it out one host at a time. The
host's half -- the signature check, the version floor, the staging, the
install and the rollback -- is Rust, and is covered by `mod tests` in
`agent/src/upgrade.rs`, because it is the half whose correctness is a security
property and it must be tested where it runs.

Two things are worth stating about what is *not* here. There is no test that a
bad signature is refused, because this process cannot tell: it holds no key,
which is the entire design. And there is no test that a rollout completes
across a fleet, because "completes" is not the property that matters -- what
matters is that it **stops**, and every test below that names a failure is
asserting the run went no further.

Nothing here touches Docker, a network or a socket.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_controller

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX
from bystack.core.graph.store import InMemoryGraphStore
from bystack.core.ports.agent import AgentDisconnected
from bystack.infra.eventbus.memory import InMemoryEventBus
from bystack.infra.releases import ReleaseError, ReleaseStore, parse_manifest
from bystack.providers.agent.provider import AgentProvider
from bystack.providers.agent.upgrade import CAP_UPGRADE, CHUNK_BYTES
from bystack.runtime import upgrade as rollout_module
from bystack.runtime.upgrade import UpgradeService, UpgradeUnavailable
from bystack.runtime.writer import PartitionWriter

ENGINE = "AAAABBBBCCCC"
ARTIFACT = b"\x7fELF" + b"not really a binary, but the right shape" * 40


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def write_release(
    directory: Path,
    *,
    version: str = "0.4.0",
    arch: str = "x86_64",
    body: bytes = ARTIFACT,
    digest: str | None = None,
    magic: str = "bystack-manifest/1",
    extra: str = "",
    name: str = "bystack-agent",
) -> Path:
    """One artifact and the two files that vouch for it.

    The signature is bytes and nothing checks them here, and that is not a
    shortcut: the Controller distributes a signature it cannot verify and could
    not have made. Every parameter above exists so a test can produce a *wrong*
    release, because those are the ones this module has an opinion about.
    """
    import hashlib

    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / f"bystack-agent-{arch}"
    binary.write_bytes(body)
    document = (
        f"{magic}\n"
        f"name {name}\n"
        f"version {version}\n"
        f"arch {arch}\n"
        f"sha256 {digest or hashlib.sha256(body).hexdigest()}\n"
        f"released_at 1755388800\n"
        f"{extra}"
    )
    binary.with_name(binary.name + ".manifest").write_text(document)
    binary.with_name(binary.name + ".manifest.sig").write_bytes(b"\x00" * 64)
    return binary


class ScriptedAgent:
    """An agent that answers upgrade offers however the test says.

    ``answer`` is called with the offer and returns the statuses to deliver, in
    order. Returning fewer than the transfer needs is how the timeout paths are
    reached, and it is the reason this is a callback rather than a canned
    reply.
    """

    def __init__(
        self,
        provider: AgentProvider,
        *,
        capabilities: tuple[str, ...] = ("commands", CAP_UPGRADE),
        architecture: str = "x86_64",
        agent_version: str = "0.3.0",
        local: bool = False,
        accept: bool = True,
        resume_from: int = 0,
        final: str = "staged",
        reason: str = "",
        fail_after: int | None = None,
    ) -> None:
        self.engine_id = ENGINE
        self.read_only = False
        self.local = local
        self.agent_version = agent_version
        self.architecture = architecture
        self.capabilities = frozenset(capabilities)
        self.sent: list[wire.Envelope] = []
        self.dead = False
        self._provider = provider
        self._accept = accept
        self._resume_from = resume_from
        self._final = final
        self._reason = reason
        self._fail_after = fail_after
        self._chunks = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, envelope: object) -> None:
        assert isinstance(envelope, wire.Envelope)
        if self.dead:
            raise AgentDisconnected("agent is gone")
        self.sent.append(envelope)

        match envelope.WhichOneof("payload"):
            case "upgrade_offer":
                offer = envelope.upgrade_offer
                self._spawn(
                    self._status(
                        offer.transfer_id,
                        "accepted" if self._accept else "refused",
                        self._reason,
                        self._resume_from,
                    )
                )
            case "upgrade_chunk":
                chunk = envelope.upgrade_chunk
                self._chunks += 1
                if self._fail_after is not None and self._chunks >= self._fail_after:
                    self._spawn(
                        self._status(chunk.transfer_id, "failed", "no space left on device")
                    )
                elif chunk.last:
                    self._spawn(self._status(chunk.transfer_id, self._final, self._reason))
            case _:
                return

    def _spawn(self, work: Any) -> None:
        # From a task rather than inline, because that is where a real answer
        # comes from: the receive loop, after the pusher has parked itself on
        # the queue. Resolving inline would test an ordering the transport
        # cannot produce.
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _status(
        self, transfer_id: str, state: str, reason: str = "", resume_from: int = 0
    ) -> None:
        await asyncio.sleep(0)
        await self._provider.on_frame(
            wire.Envelope(
                upgrade_status=wire.UpgradeStatus(
                    transfer_id=transfer_id,
                    state=state,
                    reason=reason,
                    resume_from=resume_from,
                )
            )
        )

    def chunks(self) -> list[wire.UpgradeChunk]:
        return [
            envelope.upgrade_chunk
            for envelope in self.sent
            if envelope.WhichOneof("payload") == "upgrade_chunk"
        ]

    def offers(self) -> list[wire.UpgradeOffer]:
        return [
            envelope.upgrade_offer
            for envelope in self.sent
            if envelope.WhichOneof("payload") == "upgrade_offer"
        ]


def make_provider(engine_id: str = ENGINE) -> AgentProvider:
    return AgentProvider(
        engine_id, PartitionWriter(InMemoryGraphStore(), InMemoryEventBus(), engine_id)
    )


def attach(provider: AgentProvider, agent: ScriptedAgent) -> ScriptedAgent:
    provider.attach(agent)  # type: ignore[arg-type]
    return agent


# --------------------------------------------------------------------------
# The release directory
# --------------------------------------------------------------------------


def test_a_signed_pair_is_indexed_by_architecture(tmp_path: Path) -> None:
    write_release(tmp_path, arch="x86_64")
    write_release(tmp_path, arch="aarch64", body=b"a different machine entirely")
    store = ReleaseStore(tmp_path)

    assert {release.arch for release in store.all()} == {"x86_64", "aarch64"}
    assert store.latest("x86_64") is not None
    assert store.latest("x86_64").version == "0.4.0"  # type: ignore[union-attr]
    # A fleet with no host of that shape asks, and gets an answer rather than
    # the nearest thing: sending the wrong architecture is a host that goes
    # quiet, which is the failure this whole feature exists to avoid.
    assert store.latest("sparc64") is None


def test_the_signed_document_is_carried_verbatim(tmp_path: Path) -> None:
    """What the agent verifies is the byte string that was signed.

    A Controller that re-serialized the manifest from parsed fields would send
    a document whose signature covers something else, and every host in the
    fleet would refuse it with a message about trust. So the bytes are read and
    kept, never rebuilt.
    """
    binary = write_release(tmp_path)
    release = ReleaseStore(tmp_path).latest("x86_64")
    assert release is not None
    assert release.manifest == binary.with_name(binary.name + ".manifest").read_bytes()


def test_a_release_that_would_be_refused_by_the_fleet_is_not_offered(tmp_path: Path) -> None:
    """Every one of these is a mispairing an operator makes with a `cp`.

    They are caught here, with a log line naming the file, rather than on the
    fleet one host at a time -- the agent applies the same strict rules, so a
    manifest this accepted and an agent refused would be a release the
    dashboard offers and every machine turns down.
    """
    # A digest for something else.
    write_release(tmp_path / "mismatch", digest="ab" * 32)
    assert ReleaseStore(tmp_path / "mismatch").all() == []

    # A format this Controller does not read.
    write_release(tmp_path / "future", magic="bystack-manifest/2")
    assert ReleaseStore(tmp_path / "future").all() == []

    # A field added later, which an older reader must not silently ignore.
    write_release(tmp_path / "extra", extra="expires_at 1755388800\n")
    assert ReleaseStore(tmp_path / "extra").all() == []

    # A signed something-else.
    write_release(tmp_path / "other", name="bystack-agent-experimental")
    assert ReleaseStore(tmp_path / "other").all() == []

    # A binary with no signature beside it.
    binary = write_release(tmp_path / "unsigned")
    binary.with_name(binary.name + ".manifest.sig").unlink()
    assert ReleaseStore(tmp_path / "unsigned").all() == []


def test_one_bad_file_does_not_hide_the_other_architecture(tmp_path: Path) -> None:
    """A fleet is mixed by design (ADR-0008). One mispaired manifest taking the
    other architecture's release down with it would mean an operator's mistake
    on a Pi stops them upgrading forty servers."""
    write_release(tmp_path, arch="x86_64")
    write_release(tmp_path, arch="aarch64", body=b"aarch64", digest="cd" * 32)

    held = ReleaseStore(tmp_path).all()
    assert [release.arch for release in held] == ["x86_64"]


def test_an_empty_directory_is_a_state_and_not_a_failure(tmp_path: Path) -> None:
    assert ReleaseStore(tmp_path / "nothing-here").all() == []
    assert ReleaseStore(tmp_path / "nothing-here").versions() == []


def test_the_manifest_parse_names_what_is_wrong(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="not `key value`"):
        parse_manifest(b"bystack-manifest/1\nname\n")
    with pytest.raises(ReleaseError, match="twice"):
        parse_manifest(b"bystack-manifest/1\nname bystack-agent\nname bystack-agent\n")
    with pytest.raises(ReleaseError, match="has no"):
        parse_manifest(b"bystack-manifest/1\nname bystack-agent\n")


# --------------------------------------------------------------------------
# Pushing to one host
# --------------------------------------------------------------------------


async def test_a_release_is_offered_then_sent_in_chunks(tmp_path: Path) -> None:
    write_release(tmp_path, body=b"x" * (CHUNK_BYTES + 500))
    provider = make_provider()
    agent = attach(provider, ScriptedAgent(provider))

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.staged, outcome.reason
    chunks = agent.chunks()
    assert len(chunks) == 2, "an artifact over one chunk must not arrive as one frame"
    assert chunks[0].offset == 0
    assert chunks[1].offset == CHUNK_BYTES
    assert chunks[-1].last
    assert b"".join(chunk.data for chunk in chunks) == b"x" * (CHUNK_BYTES + 500)


async def test_a_partial_transfer_is_resumed_rather_than_restarted(tmp_path: Path) -> None:
    """A reconnect mid-upgrade must not put the whole artifact back on the
    wire. On the uplinks these fleets actually have, a transfer that restarts
    on every drop is a transfer that never finishes."""
    body = b"y" * (CHUNK_BYTES * 3)
    write_release(tmp_path, body=body)
    provider = make_provider()
    agent = attach(provider, ScriptedAgent(provider, resume_from=CHUNK_BYTES * 2))

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.staged
    chunks = agent.chunks()
    assert len(chunks) == 1
    assert chunks[0].offset == CHUNK_BYTES * 2


async def test_a_refused_offer_sends_no_bytes(tmp_path: Path) -> None:
    """The point of offering before sending. A host that will refuse the
    release refuses it in one small frame, not after two megabytes of
    somebody's uplink."""
    write_release(tmp_path)
    provider = make_provider()
    agent = attach(
        provider,
        ScriptedAgent(provider, accept=False, reason="this host runs 0.5.0 already"),
    )

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.state == "refused"
    assert outcome.reason == "this host runs 0.5.0 already"
    assert agent.chunks() == []


async def test_a_failure_mid_transfer_stops_the_transfer(tmp_path: Path) -> None:
    """Continuing to push at a host that has already said no is the one thing
    worth checking for between frames."""
    write_release(tmp_path, body=b"z" * (CHUNK_BYTES * 5))
    provider = make_provider()
    agent = attach(provider, ScriptedAgent(provider, fail_after=1))

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.state == "failed"
    assert outcome.reason == "no space left on device"
    assert len(agent.chunks()) < 5


async def test_a_host_that_goes_away_answers_immediately(tmp_path: Path) -> None:
    write_release(tmp_path)
    provider = make_provider()
    agent = attach(provider, ScriptedAgent(provider))
    agent.dead = True

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert not outcome.staged
    assert outcome.state == "failed"


async def test_an_agent_without_the_capability_is_refused_by_name(tmp_path: Path) -> None:
    """Absence is the answer for "too old" and for "built with no keys", and
    the response is the same: say so, name the version, and send nothing."""
    write_release(tmp_path)
    provider = make_provider()
    agent = attach(
        provider, ScriptedAgent(provider, capabilities=("commands",), agent_version="0.2.0")
    )

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.state == "refused"
    assert "0.2.0" in (outcome.reason or "")
    assert "install-agent.sh" in (outcome.reason or "")
    assert agent.sent == []
    assert not provider.upgradable


async def test_the_wrong_architecture_never_reaches_the_wire(tmp_path: Path) -> None:
    write_release(tmp_path, arch="x86_64")
    provider = make_provider()
    agent = attach(provider, ScriptedAgent(provider, architecture="aarch64"))

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.state == "refused"
    assert "aarch64" in (outcome.reason or "")
    assert agent.sent == []


async def test_the_controllers_own_agent_is_not_upgraded_this_way(tmp_path: Path) -> None:
    """That binary lives inside the Controller's installation. Upgrading it is
    upgrading the Controller, and pushing a release to it would replace a file
    the Controller's own packaging owns."""
    write_release(tmp_path)
    provider = make_provider()
    attach(provider, ScriptedAgent(provider, local=True))

    outcome = await provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]

    assert outcome.state == "refused"
    assert not provider.upgradable


async def test_a_disconnect_frees_a_push_that_is_waiting(tmp_path: Path) -> None:
    """A rollout waiting on a host that has gone away must not sit out its
    deadline for an answer that provably cannot arrive."""
    write_release(tmp_path)
    provider = make_provider()

    class Silent(ScriptedAgent):
        async def send(self, envelope: object) -> None:
            self.sent.append(envelope)  # type: ignore[arg-type]

    agent = attach(provider, Silent(provider))
    pushing = asyncio.create_task(
        provider.push_upgrade(ReleaseStore(tmp_path).latest("x86_64"))  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    provider.detach("the host went away")

    outcome = await asyncio.wait_for(pushing, timeout=2)
    assert outcome.state == "failed"
    assert "went away" in (outcome.reason or "")
    assert agent.offers()


# --------------------------------------------------------------------------
# The staged rollout
# --------------------------------------------------------------------------


def service(tmp_path: Path, providers: dict[str, Any], **kwargs: Any) -> UpgradeService:
    return UpgradeService(ReleaseStore(tmp_path), lambda: providers, **kwargs)


async def test_a_host_already_on_the_version_is_not_touched(tmp_path: Path) -> None:
    """What makes restarting an interrupted run cheap: the answer comes from
    what each agent reported at its last Hello, not from a record."""
    write_release(tmp_path, version="0.4.0")
    current, behind = make_provider("current"), make_provider("behind")
    attach(current, ScriptedAgent(current, agent_version="0.4.0"))
    attach(behind, ScriptedAgent(behind, agent_version="0.3.0"))

    planned = service(tmp_path, {"current": current, "behind": behind}).plan("0.4.0")

    assert [provider.id for provider in planned] == ["behind"]


async def test_a_rollout_confirms_each_host_before_the_next(tmp_path: Path) -> None:
    write_release(tmp_path)
    first, second = make_provider("a"), make_provider("b")
    agents = {
        "a": attach(first, ScriptedAgent(first)),
        "b": attach(second, ScriptedAgent(second)),
    }
    providers: dict[str, Any] = {"a": first, "b": second}
    runner = service(tmp_path, providers)

    # `Hello` is the test, not the install: a host is confirmed when it comes
    # back saying it is the new version, which is what these two agents do.
    async def upgrade_and_reconnect() -> None:
        rollout = await runner.start("0.4.0")
        while rollout.running:
            current = rollout.current
            if current is not None and agents[current].sent:
                agents[current].agent_version = "0.4.0"
            await asyncio.sleep(0.01)

    await asyncio.wait_for(upgrade_and_reconnect(), timeout=10)

    rollout = runner.rollout
    assert rollout is not None
    assert rollout.state == "finished"
    assert [result.state for result in rollout.results] == ["confirmed", "confirmed"]


async def test_a_refusal_stops_the_run_rather_than_completing_it(tmp_path: Path) -> None:
    """The whole argument for a staged rollout in one assertion.

    A release the first host refuses is a release every host will refuse. The
    run stops there, so the operator finds out once instead of forty times --
    and the thirty-nine machines that were never touched are still on a version
    that works.
    """
    write_release(tmp_path)
    first, second = make_provider("a"), make_provider("b")
    attach(first, ScriptedAgent(first, accept=False, reason="not signed by a key I hold"))
    later = attach(second, ScriptedAgent(second))
    runner = service(tmp_path, {"a": first, "b": second})

    rollout = await runner.start("0.4.0")
    await asyncio.wait_for(_settle(rollout), timeout=10)

    assert rollout.state == "failed"
    assert "not signed by a key I hold" in (rollout.detail or "")
    assert [result.engine_id for result in rollout.results] == ["a"]
    assert later.sent == [], "the second host must not have been touched"


async def test_a_host_that_never_comes_back_stops_the_run_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary that starts is not an agent that works.

    The host is still covered -- it restores its previous agent by itself
    within its own probation window -- and the rollout's job at that point is
    to stop and name it.
    """
    monkeypatch.setattr(rollout_module, "CONFIRM_TIMEOUT", 0.2)
    monkeypatch.setattr(rollout_module, "CONFIRM_POLL", 0.05)
    write_release(tmp_path)
    first, second = make_provider("a"), make_provider("b")
    attach(first, ScriptedAgent(first))  # stages, and stays on 0.3.0
    later = attach(second, ScriptedAgent(second))
    runner = service(tmp_path, {"a": first, "b": second})

    rollout = await runner.start("0.4.0")
    await asyncio.wait_for(_settle(rollout), timeout=10)

    assert rollout.state == "failed"
    assert "has not come back" in (rollout.detail or "")
    assert "restore its previous agent" in (rollout.detail or "")
    assert later.sent == []


async def test_a_host_that_left_between_planning_and_its_turn_is_stepped_over(
    tmp_path: Path,
) -> None:
    """Ordinary on a fleet of laptops and home servers, and it says nothing
    about the release -- so it is recorded and stepped over rather than treated
    as the failure that stops everything."""
    write_release(tmp_path)
    gone, present = make_provider("a"), make_provider("b")
    attach(gone, ScriptedAgent(gone))
    agent = attach(present, ScriptedAgent(present))
    runner = service(tmp_path, {"a": gone, "b": present})
    rollout = await runner.start("0.4.0")

    gone.detach("closed its lid")
    agent.agent_version = "0.4.0"
    await asyncio.wait_for(_settle(rollout), timeout=10)

    assert rollout.state == "finished"
    states = {result.engine_id: result.state for result in rollout.results}
    assert states == {"a": "skipped", "b": "confirmed"}


async def test_a_read_only_controller_will_not_push_anything(tmp_path: Path) -> None:
    """Read-only is about mutation, and replacing the executable on somebody
    else's machine is the largest mutation in the product."""
    write_release(tmp_path)
    provider = make_provider()
    attach(provider, ScriptedAgent(provider))
    runner = service(tmp_path, {ENGINE: provider}, read_only=True)

    with pytest.raises(UpgradeUnavailable, match="read-only"):
        await runner.start("0.4.0")


async def test_starting_with_nothing_signed_says_where_to_put_it(tmp_path: Path) -> None:
    provider = make_provider()
    attach(provider, ScriptedAgent(provider))
    runner = service(tmp_path / "empty", {ENGINE: provider})

    with pytest.raises(UpgradeUnavailable, match="sign-agent.py"):
        await runner.start()


async def test_two_rollouts_at_once_are_refused(tmp_path: Path) -> None:
    write_release(tmp_path)
    provider = make_provider()
    attach(provider, ScriptedAgent(provider))
    runner = service(tmp_path, {ENGINE: provider})

    await runner.start("0.4.0")
    with pytest.raises(UpgradeUnavailable, match="already running"):
        await runner.start("0.4.0")
    await runner.aclose()


async def _settle(rollout: Any, limit: float = 10.0) -> None:
    """Wait for a run to reach a terminal state.

    Polled rather than signalled, and deliberately: the rollout is deliberately
    not an event source. It reports itself through one object that a route
    reads and a panel polls, and a test that waited on an `asyncio.Event` would
    be waiting on a mechanism the product does not have.
    """
    deadline = time.monotonic() + limit
    while rollout.running and time.monotonic() < deadline:  # noqa: ASYNC110 - see above
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------


def test_the_releases_route_names_the_directory_when_it_is_empty(tmp_path: Path) -> None:
    """The empty answer is the common one. An empty list with no path is a
    feature that looks broken rather than one with nothing in it yet."""
    controller = make_controller(tmp_path)
    with TestClient(controller.ui) as client:
        answer = client.get(f"{API_PREFIX}/agents/releases").json()

    assert answer["releases"] == []
    assert answer["directory"].endswith("releases")


def test_a_rollout_with_nobody_to_send_to_is_a_409_that_explains(tmp_path: Path) -> None:
    controller = make_controller(tmp_path, read_only=False)
    write_release(Path(controller.settings.agents.releases_path))

    with TestClient(controller.ui) as client:
        answer = client.post(f"{API_PREFIX}/agents/upgrades", json={})

    assert answer.status_code == 409
    assert "install-agent.sh" in answer.json()["detail"]


def test_cancelling_a_rollout_that_never_ran_is_a_404(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)
    with TestClient(controller.ui) as client:
        assert client.post(f"{API_PREFIX}/agents/upgrades/cancel").status_code == 404
        assert client.get(f"{API_PREFIX}/agents/upgrades").json() is None
