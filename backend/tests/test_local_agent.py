"""Zero-config startup: the agent the Controller runs for its own machine.

`docs/MIGRATION.md` §4. Deleting the agentless path took away the property
that `python -m bystack` with no configuration manages the local engine, and
this is how it comes back: the Controller spawns the same agent binary a
managed host runs, over a unix socket instead of TLS.

Two claims are worth testing and they pull in opposite directions.

The first is that the local path is **not a second implementation**. Below the
admission line it is the same route, the same frames and the same ingest, so a
graph filled locally is indistinguishable from one filled by an enrolled host.

The second is that its admission rule stays **where it was put**. A connection
with no certificate is admitted on the unix socket and refused on the fleet's
listener, and nothing about the first can make the second true. That is why
the assertions below are mostly pairs.

Nothing here spawns a process or opens a real socket -- `bystack.conformance
.local` does that, with the real binary. This file is about the decisions.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect
from tests.conftest import Controller, make_controller

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX
from bystack.config import LocalAgentConfig, Settings
from bystack.core.identity import container_urn, host_urn
from bystack.runtime.localagent import LocalAgent, LocalAgentState, _find_binary

AGENT_PATH = f"{API_PREFIX}/agents/connect"
ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE = "AAAABBBBCCCC"
C1 = "c" * 64


def hello_frame(engine_id: str = ENGINE_ID, *, read_only: bool = False) -> bytes:
    return wire.Envelope(
        hello=wire.Hello(
            agent_version="9.9.9",
            engine_id=engine_id,
            engine=wire.EngineInfo(id=engine_id, name="this-machine", server_version="29.6.0"),
            read_only=read_only,
            unix_time=int(time.time()),
        )
    ).SerializeToString()


def container_sync() -> bytes:
    return wire.Envelope(
        sync=wire.Sync(
            slice=wire.SLICE_CONTAINER,
            entities=[
                wire.Entity(
                    id=C1,
                    container=wire.Container(
                        id=C1,
                        names=["/shop-web-1"],
                        image="nginx:latest",
                        image_id="sha256:abc",
                        state="running",
                        status_text="Up 3 hours",
                    ),
                )
            ],
        )
    ).SerializeToString()


def decode(raw: bytes) -> wire.Envelope:
    envelope = wire.Envelope()
    envelope.ParseFromString(raw)
    return envelope


@pytest.fixture
def controller(tmp_path: Path) -> Controller:
    return make_controller(tmp_path)


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_the_local_socket_admits_an_agent_with_no_certificate(controller: Controller) -> None:
    """The file mode is the credential.

    A peer that can open a 0600 socket in a 0700 directory is already this
    user on this machine, with the access to the Docker socket that a
    certificate would be protecting. A CA round trip between a parent process
    and the child it forked would be ceremony, not a control.
    """
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        ack = decode(agent.receive_bytes()).hello_ack
        assert ack.accepted is True


def test_the_same_connection_is_refused_on_the_fleets_listener(controller: Controller) -> None:
    """The pair that matters.

    Identical frames, no certificate, two doors. If this ever passes on both,
    the local path has become a way to skip ADR-0011 rather than a transport
    underneath it.
    """
    with controller.agent_client(None).websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        ack = decode(agent.receive_bytes()).hello_ack
        assert ack.accepted is False
        assert "certificate" in ack.reason
        # Terminal: no certificate is not a condition that improves by coming
        # back, unlike awaiting approval.
        assert ack.retry is False


def test_the_fleets_listener_does_not_serve_the_local_route(controller: Controller) -> None:
    """Two apps, not one app with a flag.

    The local route exists on the local app only, so a misconfiguration cannot
    put the certificate-free admission rule on the port that faces the fleet:
    there is no such route there to reach.
    """
    # Asserted by dialling rather than by reading the route table, because the
    # question is what the socket answers.
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        assert decode(agent.receive_bytes()).hello_ack.accepted is True

    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - two contexts, one assertion
        with controller.local_client().websocket_connect(f"{API_PREFIX}/agents/enroll"):
            pass


def test_a_local_agent_that_reports_no_engine_id_is_refused(controller: Controller) -> None:
    """Skipping the certificate does not skip the identity.

    The engine id scopes the partition. An agent that has not named one has
    nowhere to write, and admitting it would mean writing observations into
    whatever partition came first.
    """
    frame = wire.Envelope(hello=wire.Hello(agent_version="9.9.9", engine_id="")).SerializeToString()
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(frame)
        with pytest.raises(WebSocketDisconnect):
            agent.receive_bytes()


def test_a_local_agent_is_never_offered_a_renewal(controller: Controller) -> None:
    """There is no certificate to renew, so there is nothing to offer.

    A RenewalOffer here would be answered with a CSR the Controller would
    refuse -- an exchange with no outcome, repeated on every connection.
    """
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        assert decode(agent.receive_bytes()).WhichOneof("payload") == "hello_ack"
        agent.send_bytes(container_sync())

    verdict = controller.context.trust.admit_local(ENGINE_ID)
    assert verdict.renew is False  # type: ignore[union-attr]
    assert verdict.local is True  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# The graph does not know the difference
# --------------------------------------------------------------------------


def test_a_locally_filled_graph_is_an_ordinary_graph(controller: Controller) -> None:
    """The whole argument for routing the local case through the same code.

    Same URNs, same partition, same host node, same logical layer. If this
    needed its own assertions the Controller would have grown a second
    discovery path, which is exactly what `docs/MIGRATION.md` §4 rejected.
    """
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        agent.receive_bytes()
        agent.send_bytes(container_sync())

        with TestClient(controller.ui) as browser:
            snapshot = browser.get(f"{API_PREFIX}/graph").json()

    urns = {node["urn"] for node in snapshot["nodes"]}
    assert host_urn(ENGINE) in urns
    assert container_urn(ENGINE, C1) in urns
    # And it is in this host's partition, like any other host's containers.
    assert {node["source"] for node in snapshot["nodes"]} == {ENGINE}


def test_the_local_host_is_listed_without_being_enrolled(controller: Controller) -> None:
    """It appears in the fleet, and it is not in the registry.

    A registry entry would be a durable record of operator intent that no
    operator expressed (ADR-0001), and it would outlive the agent it described
    -- a Controller moved to another machine would carry a row for the old one
    forever. Reading it off the live provider means it appears when the agent
    connects and is gone when it does not.
    """
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        agent.receive_bytes()

        with TestClient(controller.ui) as browser:
            listed = browser.get(f"{API_PREFIX}/agents").json()

    assert [(a["engine_id"], a["status"], a["connected"]) for a in listed] == [
        (ENGINE, "local", True)
    ]
    # Reported from the connection, not from an enrollment that never happened.
    assert listed[0]["agent_version"] == "9.9.9"
    assert listed[0]["certificate_expires_at"] == 0
    assert controller.context.trust.registry.all() == []


def test_the_local_host_disappears_when_its_agent_does(controller: Controller) -> None:
    """No record to go stale.

    The host's *topology* stays -- DEGRADED means "the last thing it told us",
    which is not the same as nothing -- but the fleet row does not, because
    there is nothing enrolled behind it to describe.
    """
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        agent.receive_bytes()
        agent.send_bytes(container_sync())

    with TestClient(controller.ui) as browser:
        assert browser.get(f"{API_PREFIX}/agents").json() == []
        # The graph it filled is still there, which is the point of DEGRADED.
        snapshot = browser.get(f"{API_PREFIX}/graph").json()
    assert container_urn(ENGINE, C1) in {node["urn"] for node in snapshot["nodes"]}


def test_a_read_only_local_agent_is_believed(controller: Controller) -> None:
    """Advertised at Hello and honoured, exactly as a remote agent's is.

    The Controller passes `--read-only` to the child when it is configured
    read-only, and this is the other end of that: what the agent says about
    itself is what the UI is told, so actions are disabled rather than offered
    and bounced.
    """
    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame(read_only=True))
        agent.receive_bytes()
        agent.send_bytes(container_sync())

        with TestClient(controller.ui) as browser:
            actions = browser.get(
                f"{API_PREFIX}/commands/actions", params={"urn": container_urn(ENGINE, C1)}
            ).json()

    assert actions["commands"] == []
    assert actions["reason"]


# --------------------------------------------------------------------------
# Whether it can run at all, and saying why not
# --------------------------------------------------------------------------


def test_disabled_is_reported_as_a_choice_and_not_as_a_fault(tmp_path: Path) -> None:
    settings = Settings(local_agent=LocalAgentConfig(enabled=False))
    status = LocalAgent(settings).status
    assert status.state is LocalAgentState.DISABLED
    assert status.ok is False


def test_a_machine_with_no_docker_socket_says_so(tmp_path: Path) -> None:
    """The difference between "nothing to manage" and "something is broken".

    A Controller on a host that runs no containers is an ordinary deployment.
    It must not look like a failed install, and the empty canvas needs a
    sentence next to it either way.
    """
    settings = Settings(
        local_agent=LocalAgentConfig(docker_socket=str(tmp_path / "nothing-here.sock"))
    )
    status = LocalAgent(settings).status

    assert status.state is LocalAgentState.UNAVAILABLE
    assert "nothing-here.sock" in status.detail
    assert "enrolling" in status.detail  # and other hosts still can


def test_a_missing_binary_names_everywhere_it_looked(tmp_path: Path, monkeypatch: Any) -> None:
    """The failure an operator is most likely to hit, and the least guessable.

    "no agent found" with no paths sends someone to read the source. The list
    is the fix, and it is also how the search order is pinned.
    """
    monkeypatch.setattr("bystack.runtime.localagent.BUNDLED", tmp_path / "bundled")
    monkeypatch.setattr("bystack.runtime.localagent.IN_TREE", tmp_path / "in-tree")
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.delenv("BYSTACK_AGENT_BINARY", raising=False)

    binary, reason = _find_binary("")

    assert binary is None
    assert "bundled" in reason and "in-tree" in reason
    assert "cargo build --release" in reason


def test_a_configured_binary_is_an_assertion_and_is_not_fallen_back_from(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An operator who named a binary and silently got another one would have
    no way to discover it -- which is the whole reason this path is separate
    from the search."""
    working = tmp_path / "real-agent"
    working.write_text("#!/bin/sh\n")
    working.chmod(0o755)
    monkeypatch.setattr("bystack.runtime.localagent.IN_TREE", working)

    binary, reason = _find_binary(str(tmp_path / "typo"))

    assert binary is None
    assert "typo" in reason


def test_the_environment_override_wins_over_the_search(tmp_path: Path, monkeypatch: Any) -> None:
    chosen = tmp_path / "chosen"
    chosen.write_text("#!/bin/sh\n")
    chosen.chmod(0o755)
    other = tmp_path / "other"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)

    monkeypatch.setenv("BYSTACK_AGENT_BINARY", str(chosen))
    monkeypatch.setattr("bystack.runtime.localagent.BUNDLED", other)

    assert _find_binary("") == (chosen, "")


# --------------------------------------------------------------------------
# The socket
# --------------------------------------------------------------------------


def test_the_socket_is_created_unreadable_by_anyone_else(tmp_path: Path) -> None:
    """The mode *is* the authentication (`runtime/trust.py`, `admit_local`).

    Created 0600 by umask rather than chmod'd afterwards: a bind-then-chmod
    leaves a window in which anything on the machine could connect and claim
    to be this host's agent. uvicorn opens a unix socket world-writable, which
    is why the Controller binds this one itself.
    """
    engine = tmp_path / "docker.sock"
    engine.touch()
    settings = Settings(
        local_agent=LocalAgentConfig(
            docker_socket=str(engine),
            socket=str(tmp_path / "state" / "local-agent.sock"),
            binary=str(_executable(tmp_path / "agent")),
        )
    )
    agent = LocalAgent(settings)

    listener = agent.bind()
    assert listener is not None
    try:
        assert agent.socket_path.stat().st_mode & 0o777 == 0o600
        assert agent.socket_path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        listener.close()


def test_nothing_is_bound_when_there_is_nothing_to_run(tmp_path: Path) -> None:
    """No socket for a listener with no client. A path in the state directory
    that nothing ever dials is a thing to explain later."""
    # Pointed at `tmp_path` rather than left at the default, because the
    # default is this user's real state directory -- and on a machine that has
    # ever run the Controller, the assertion below would be about a socket
    # some other process left there.
    settings = Settings(
        local_agent=LocalAgentConfig(enabled=False, socket=str(tmp_path / "local-agent.sock"))
    )
    agent = LocalAgent(settings)
    assert agent.bind() is None
    assert not agent.socket_path.exists()


def test_a_socket_left_by_a_killed_controller_is_replaced(tmp_path: Path) -> None:
    """`bind` on an existing path fails with EADDRINUSE, and a Controller that
    was killed rather than stopped leaves one behind. Removing it is safe
    precisely because nothing else may write in that directory."""
    engine = tmp_path / "docker.sock"
    engine.touch()
    stale = tmp_path / "state" / "local-agent.sock"
    stale.parent.mkdir()
    stale.touch()

    settings = Settings(
        local_agent=LocalAgentConfig(
            docker_socket=str(engine),
            socket=str(stale),
            binary=str(_executable(tmp_path / "agent")),
        )
    )
    listener = LocalAgent(settings).bind()
    assert listener is not None
    listener.close()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_zero_config_manages_this_machine_and_waits_for_the_rest() -> None:
    """What `python -m bystack` with no arguments means.

    The two flags answer different questions and keep different defaults: the
    local agent binds no port and admits no stranger, and the fleet listener
    does both.
    """
    settings = Settings.default()
    assert settings.local_agent.enabled is True
    assert settings.agents.enabled is False


def test_the_terms_route_explains_an_empty_first_run(tmp_path: Path) -> None:
    """An empty canvas with no sentence next to it is the regression this
    whole path exists to close, and the fleet panel is where the sentence
    goes."""
    controller = make_controller(tmp_path, local_agent=False)
    with TestClient(controller.ui) as browser:
        terms = browser.get(f"{API_PREFIX}/agents/enrollment").json()

    assert terms["local_agent"]["state"] == "disabled"
    assert terms["local_agent"]["detail"]


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_blank_docker_socket_is_refused_at_load() -> None:
    """`Path("")` is the current directory, which exists.

    A blank value would therefore sail through the "is there an engine here"
    check and spawn an agent pointed at a directory, which fails later and
    somewhere less informative. Refused where the message can name the field.
    """
    with pytest.raises(ValidationError):
        LocalAgentConfig(docker_socket="")


def test_a_host_that_is_enrolled_and_locally_managed_is_listed_once(tmp_path: Path) -> None:
    """Found by running the Controller on a machine already in its own fleet.

    The two sets overlap: enrol a host, then run the Controller on it, and it
    is both an enrollment record and a local session. Concatenating them listed
    it twice -- and the second row claimed the first one's connection, because
    `connected` is computed from the one provider they share.

    Merged on the engine id, which is the right key for the same reason it is
    the partition key: under ADR-0011 "which host is this" and "which agent is
    this" are one question.
    """
    controller = make_controller(tmp_path, auto_approve=True)
    controller.enroll(ENGINE_ID)

    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        agent.receive_bytes()

        with TestClient(controller.ui) as browser:
            listed = browser.get(f"{API_PREFIX}/agents").json()

    assert len(listed) == 1
    row = listed[0]
    # The enrollment record wins the row, so the certificate stays visible and
    # revocable; `local` says which agent is actually attached.
    assert (row["engine_id"], row["status"], row["local"]) == (ENGINE, "approved", True)
    assert row["certificate_expires_at"] > 0


def test_an_unenrolled_local_host_says_so_in_its_status(tmp_path: Path) -> None:
    """The ordinary case, and the one that must not borrow the fleet's words."""
    controller = make_controller(tmp_path)

    with controller.local_client().websocket_connect(AGENT_PATH) as agent:
        agent.send_bytes(hello_frame())
        agent.receive_bytes()

        with TestClient(controller.ui) as browser:
            listed = browser.get(f"{API_PREFIX}/agents").json()

    assert [(a["status"], a["local"]) for a in listed] == [("local", True)]
