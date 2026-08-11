"""Agent trust: mTLS, join tokens, Engine ID as identity (ADR-0011).

The ADR ends on a requirement rather than a decision:

> Every one of these paths -- expired token, reused token, wrong Engine ID,
> revoked certificate, unapproved agent, skewed clock -- needs a test
> asserting the connection is *refused*. Authentication code that is only
> tested on the success path is not tested.

This file is that. Everything here runs against the real CA, real CSRs and
real certificates, with no listener and no handshake: `runtime/trust.py` is
where the decisions live precisely so that each one can be provoked without a
socket, and `conftest.WithClientCertificate` supplies the peer certificate
through the same ASGI extension TLS would.

What is *not* covered here, and cannot be: that OpenSSL rejects a certificate
signed by someone else during the handshake. That check is the TLS library's
and testing it would be testing OpenSSL. What is covered is that the layer
above refuses the same certificate anyway -- because a security property that
holds only as long as the layer below is configured correctly is one bad
deployment away from not holding at all.
"""

from __future__ import annotations

import base64
import datetime as dt
import stat
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.conftest import Controller, make_controller, make_csr

from bystack import __version__
from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX
from bystack.api.tls import _tls_extension, peer_certificate_pem
from bystack.infra.agentca import AgentCA, AgentStatus, CertificateError, EnrollmentRegistry
from bystack.infra.agentca.tokens import JoinTokenStore, Outcome
from bystack.runtime.trust import AgentTrust, Refusal, Refused

AGENT_PATH = f"{API_PREFIX}/agents/connect"
ENROLL_PATH = f"{API_PREFIX}/agents/enroll"
ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE = "AAAABBBBCCCC"
OTHER_ENGINE = "DDDDEEEEFFFF"


def hello(engine_id: str = ENGINE_ID, *, unix_time: int | None = None) -> bytes:
    return wire.Envelope(
        hello=wire.Hello(
            agent_version="0.1.0",
            engine_id=engine_id,
            engine=wire.EngineInfo(id=engine_id, name="lab-node-01"),
            unix_time=int(time.time()) if unix_time is None else unix_time,
        )
    ).SerializeToString()


def decode(raw: bytes) -> wire.Envelope:
    envelope = wire.Envelope()
    envelope.ParseFromString(raw)
    return envelope


def _await_frame(socket: object, payload: str, limit: int = 5) -> wire.Envelope:
    """Read until a frame of this kind arrives.

    The handshake sends more than one thing after `HelloAck` -- a watch list
    always, a renewal offer when one is due -- and their order is not part of
    the contract. Bounded rather than unbounded: a test that never finds its
    frame must fail rather than hang CI.
    """
    for _ in range(limit):
        envelope = decode(socket.receive_bytes())  # type: ignore[attr-defined]
        if envelope.WhichOneof("payload") == payload:
            return envelope
    raise AssertionError(f"no {payload} frame in the first {limit} frames")


def refusal(
    controller: Controller,
    certificate: str | None,
    *,
    engine_id: str = ENGINE_ID,
    unix_time: int | None = None,
) -> wire.HelloAck:
    """Connect, expect to be turned away, and return what we were told.

    A `HelloAck` rather than a bare close, because a close code carries no
    diagnosis -- and this is the frame in which an operator finds out their
    host is awaiting approval rather than broken.
    """
    client = controller.agent_client(certificate)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello(engine_id, unix_time=unix_time))
        ack = decode(socket.receive_bytes()).hello_ack
    assert not ack.accepted
    return ack


# --------------------------------------------------------------------------
# Join tokens
# --------------------------------------------------------------------------


def test_a_token_carries_the_ca_fingerprint(controller: Controller) -> None:
    """This is what makes enrollment an authenticated exchange rather than
    trust-on-first-use: the agent knows what the Controller's CA looks like
    before it sends the secret, so it can refuse to hand it to whatever
    answered the address."""
    minted = controller.context.trust.mint_token(dt.timedelta(minutes=15))
    scheme, fingerprint, secret = minted.token.split(".")

    assert scheme == "bst1"
    assert fingerprint == controller.context.trust.ca.fingerprint
    assert secret


def test_an_expired_token_is_refused(controller: Controller) -> None:
    trust = controller.context.trust
    token = trust.mint_token(dt.timedelta(seconds=-1))

    outcome = trust.enroll(
        join_token=token.token, engine_id=ENGINE_ID, csr_pem=make_csr()[0], agent_version="0.1.0"
    )

    assert not outcome.accepted
    assert "expired" in outcome.reason
    assert not outcome.certificate_pem


def test_a_token_is_single_use(controller: Controller) -> None:
    """It grants exactly one capability -- obtain one certificate -- once. A
    token recovered from a shell history after it was used is worth nothing,
    which is the entire argument for this credential existing."""
    trust = controller.context.trust
    token = trust.mint_token(dt.timedelta(minutes=15))

    first = trust.enroll(
        join_token=token.token, engine_id=ENGINE_ID, csr_pem=make_csr()[0], agent_version="0.1.0"
    )
    second = trust.enroll(
        join_token=token.token, engine_id=OTHER_ENGINE, csr_pem=make_csr()[0], agent_version="0.1.0"
    )

    assert first.accepted
    assert not second.accepted
    assert "already used" in second.reason
    # And the replay left nothing behind: a refused enrollment must not
    # register a host, or a burned token would still buy a pending agent.
    assert trust.registry.get(OTHER_ENGINE) is None


def test_an_unknown_token_is_refused(controller: Controller) -> None:
    trust = controller.context.trust
    forged = f"bst1.{trust.ca.fingerprint}.{'A' * 43}"

    outcome = trust.enroll(
        join_token=forged, engine_id=ENGINE_ID, csr_pem=make_csr()[0], agent_version="0.1.0"
    )

    assert not outcome.accepted
    assert "unknown" in outcome.reason


def test_a_token_from_another_controller_is_refused(controller: Controller, tmp_path: Path) -> None:
    """Named rather than reported as an unknown secret, because it is the
    likeliest way a real operator gets this wrong: two Controllers, one
    terminal, the wrong scrollback."""
    elsewhere = JoinTokenStore("f" * 64)
    outcome = controller.context.trust.enroll(
        join_token=elsewhere.mint(dt.timedelta(minutes=15)).token,
        engine_id=ENGINE_ID,
        csr_pem=make_csr()[0],
        agent_version="0.1.0",
    )

    assert not outcome.accepted


def test_a_malformed_token_is_refused(controller: Controller) -> None:
    outcome = controller.context.trust.enroll(
        join_token="not-a-token", engine_id=ENGINE_ID, csr_pem=make_csr()[0], agent_version="0.1.0"
    )

    assert not outcome.accepted
    assert "malformed" in outcome.reason


def test_a_burned_token_is_still_burned_after_it_expires() -> None:
    """The two refusals differ and both must stay refusals. A store that only
    remembered live tokens would answer "unknown" for a replayed one, which
    reads like a typo rather than the thing it is."""
    tokens = JoinTokenStore("a" * 64)
    minted = tokens.mint(dt.timedelta(minutes=15))

    assert tokens.redeem(minted.token) is Outcome.REDEEMED
    assert tokens.redeem(minted.token) is Outcome.USED


# --------------------------------------------------------------------------
# What the CA will and will not sign
# --------------------------------------------------------------------------


def test_the_subject_comes_from_the_engine_id_not_the_csr(controller: Controller) -> None:
    """A CSR proves one thing: that its sender holds the matching private key.
    Treating a name inside it as an assertion about identity is how a CA
    issues a certificate for someone else's name."""
    csr_pem, _ = make_csr(common_name="i-am-the-other-host")
    certificate = controller.enroll(ENGINE_ID, csr_pem=csr_pem)

    subject = x509.load_pem_x509_certificate(certificate.encode()).subject
    assert subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == ENGINE
    # Normalized, too: a pre-25.0 host must not end up with one spelling of
    # its identity in the certificate and another in every URN.
    assert ":" not in str(subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)


def test_an_agent_certificate_is_client_auth_only(controller: Controller) -> None:
    """An agent credential that could also serve TLS is a credential for
    standing up something that looks like a Controller."""
    certificate = controller.enroll(ENGINE_ID)
    cert = x509.load_pem_x509_certificate(certificate.encode())

    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [x509.ExtendedKeyUsageOID.CLIENT_AUTH]
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_a_csr_with_a_broken_signature_is_refused(controller: Controller) -> None:
    """The signature is the only thing a CSR proves -- that its sender holds
    the private key for the public key inside. A CA that skips the check will
    sign a key its presenter does not hold."""
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
        .sign(key, hashes.SHA256())
    )
    # Flip the last byte of the DER, which is inside the signature value: the
    # structure still parses, so this reaches `is_signature_valid` rather than
    # bouncing off the decoder and passing the test for the wrong reason.
    der = csr.public_bytes(serialization.Encoding.DER)
    tampered = _pem_csr(der[:-1] + bytes([der[-1] ^ 0xFF]))

    assert x509.load_pem_x509_csr(tampered.encode()) is not None
    with pytest.raises(CertificateError, match="not signed by its own key"):
        controller.context.trust.ca.issue_agent_certificate(tampered, ENGINE, 90)


def test_an_unreadable_csr_is_refused(controller: Controller) -> None:
    with pytest.raises(CertificateError, match="unreadable"):
        controller.context.trust.ca.issue_agent_certificate("not a csr", ENGINE, 90)


def _pem_csr(der: bytes) -> str:
    body = base64.encodebytes(der).decode()
    return f"-----BEGIN CERTIFICATE REQUEST-----\n{body}-----END CERTIFICATE REQUEST-----\n"


def test_a_weak_key_is_refused(controller: Controller) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
        .sign(key, hashes.SHA256())
    )

    with pytest.raises(CertificateError, match="1024 bits"):
        controller.context.trust.ca.issue_agent_certificate(
            csr.public_bytes(serialization.Encoding.PEM).decode(), ENGINE, 90
        )


def test_the_ca_private_key_is_not_readable_by_anyone_else(tmp_path: Path) -> None:
    """The mode is set before the bytes exist, not after. `open` then `chmod`
    leaves a window in which the fleet's trust root is on disk under whatever
    umask happened to be in force."""
    AgentCA.open(tmp_path)

    key = tmp_path / "agent-ca.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_the_ca_is_created_once_and_reopened(tmp_path: Path) -> None:
    """Reopening must not mint a new CA. It would invalidate every certificate
    in the fleet on a restart, which is the loudest possible way to discover
    that key custody is a thing this project now has."""
    first = AgentCA.open(tmp_path)
    second = AgentCA.open(tmp_path)

    assert first.fingerprint == second.fingerprint


# --------------------------------------------------------------------------
# Admission: the refusal paths, one by one
# --------------------------------------------------------------------------


def test_a_connection_with_no_certificate_is_refused(controller: Controller) -> None:
    """The case that must fail closed hardest. A listener that cannot see the
    peer's certificate has no way to know who is calling, and answering
    "probably fine" there makes the whole of ADR-0011 decorative."""
    controller.enroll(ENGINE_ID)

    ack = refusal(controller, None)

    assert Refusal.NO_CERTIFICATE.value in ack.reason
    assert not ack.retry


def test_an_agent_claiming_another_hosts_engine_id_is_refused(controller: Controller) -> None:
    """"Which host is this" and "which agent is this" are the same question
    with the same answer (ADR-0002). An agent whose Hello disagrees with its
    own certificate is claiming to be a host it is not."""
    certificate = controller.enroll(ENGINE_ID)

    ack = refusal(controller, certificate, engine_id=OTHER_ENGINE)

    assert Refusal.IDENTITY_MISMATCH.value in ack.reason
    assert not ack.retry
    assert controller.context.collector.providers == {}


def test_an_unenrolled_certificate_is_refused(controller: Controller, tmp_path: Path) -> None:
    """A certificate from a CA that is not ours, for a host that is not
    enrolled. TLS would refuse this at the handshake; the layer above refuses
    it too, because a property that holds only while the layer below is
    configured correctly is one bad deployment away from not holding."""
    stranger = AgentCA.open(tmp_path / "elsewhere")
    csr_pem, _ = make_csr()
    forged = stranger.issue_agent_certificate(csr_pem, ENGINE, 90).certificate_pem

    ack = refusal(controller, forged)

    assert Refusal.UNKNOWN.value in ack.reason
    assert not ack.retry


def test_a_foreign_certificate_for_an_enrolled_host_is_refused(
    controller: Controller, tmp_path: Path
) -> None:
    """The stronger version: the same engine id, genuinely enrolled here, but
    a certificate this CA never issued. The serial is what gives it away."""
    controller.enroll(ENGINE_ID)
    stranger = AgentCA.open(tmp_path / "elsewhere")
    forged = stranger.issue_agent_certificate(make_csr()[0], ENGINE, 90).certificate_pem

    ack = refusal(controller, forged)

    assert Refusal.SUPERSEDED.value in ack.reason
    assert not ack.retry


def test_an_unapproved_agent_is_refused_and_told_to_come_back(controller: Controller) -> None:
    """A valid token yields a certificate; approval is the separate question
    of whether that agent contributes to the graph. Leaving it separate means
    a stolen token produces a visible pending agent instead of a silent
    managed host."""
    certificate = controller.enroll(ENGINE_ID)

    ack = refusal(controller, certificate)

    assert Refusal.PENDING.value in ack.reason
    # The one refusal an agent must keep retrying: it is waiting on a person
    # clicking approve, not on anything happening on the host.
    assert ack.retry
    assert controller.context.trust.registry.get(ENGINE).status is AgentStatus.PENDING


def test_a_revoked_agent_is_refused_and_told_to_stop(controller: Controller) -> None:
    """Checked at connection time against the enrolled-agent list, which is
    why there is no CRL to publish and nothing to wait for."""
    certificate = controller.enroll(ENGINE_ID)
    controller.context.trust.registry.approve(ENGINE)
    controller.context.trust.registry.revoke(ENGINE)

    ack = refusal(controller, certificate)

    assert Refusal.REVOKED.value in ack.reason
    # Nothing on the host changes this, so an agent that kept retrying would
    # be a log line a minute until someone noticed.
    assert not ack.retry


def test_a_skewed_clock_is_refused_with_the_actual_diagnosis(controller: Controller) -> None:
    """Certificate validation is time-sensitive, and a badly wrong clock fails
    as a generic TLS error that says nothing useful. Catching it here, while
    the certificate is still valid, is worth hours (ADR-0011, Consequences)."""
    certificate = controller.enroll(ENGINE_ID)
    controller.context.trust.registry.approve(ENGINE)

    ack = refusal(controller, certificate, unix_time=int(time.time()) + 3600)

    assert Refusal.CLOCK_SKEW.value in ack.reason
    assert "NTP" in ack.reason
    # Fixable without anyone visiting the host: the NTP daemon may correct it
    # a minute from now.
    assert ack.retry


def test_an_agent_too_old_to_report_its_clock_still_connects(controller: Controller) -> None:
    """Mixed-version fleets are a normal operating state, not a migration
    window (ADR-0008). Refusing an unset field would make adding one a
    fleet-wide outage."""
    certificate = controller.enroll(ENGINE_ID)
    controller.context.trust.registry.approve(ENGINE)

    client = controller.agent_client(certificate)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello(unix_time=0))
        ack = decode(socket.receive_bytes()).hello_ack

    assert ack.accepted


def test_re_enrollment_supersedes_the_previous_certificate(controller: Controller) -> None:
    """A fresh token is the operator saying the old credential should stop
    working -- a host that lost its key, or was rebuilt. That is what
    distinguishes enrollment from renewal."""
    old = controller.enroll(ENGINE_ID)
    controller.context.trust.registry.approve(ENGINE)
    new = controller.enroll(ENGINE_ID)

    assert refusal(controller, old).reason.startswith(Refusal.SUPERSEDED.value)
    client = controller.agent_client(new)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello())
        assert decode(socket.receive_bytes()).hello_ack.accepted


def test_re_enrolling_a_revoked_host_does_not_un_revoke_it(controller: Controller) -> None:
    """A token is not an appeal. Whoever holds a revoked host's shell can also
    redeem an outstanding token, and the revocation would quietly undo
    itself."""
    controller.enroll(ENGINE_ID)
    controller.context.trust.registry.revoke(ENGINE)

    certificate = controller.enroll(ENGINE_ID)

    assert not refusal(controller, certificate).retry
    assert controller.context.trust.registry.get(ENGINE).status is AgentStatus.REVOKED


# --------------------------------------------------------------------------
# Enrollment over the wire
# --------------------------------------------------------------------------


def test_enrollment_over_the_socket_yields_a_usable_certificate(tmp_path: Path) -> None:
    """The whole first-contact exchange, on the transport the agent actually
    uses: one frame up, one frame down, no client certificate, no HTTP
    client."""
    controller = make_controller(tmp_path, auto_approve=True)
    token = controller.context.trust.mint_token(dt.timedelta(minutes=15))
    csr_pem, _ = make_csr()

    client = controller.agent_client(None)
    with client, client.websocket_connect(ENROLL_PATH) as socket:
        socket.send_bytes(
            wire.Envelope(
                enroll_request=wire.EnrollRequest(
                    join_token=token.token,
                    engine_id=ENGINE_ID,
                    csr_pem=csr_pem,
                    agent_version="0.1.0",
                )
            ).SerializeToString()
        )
        response = decode(socket.receive_bytes()).enroll_response

    assert response.accepted
    assert not response.pending_approval
    assert response.ca_pem == controller.context.trust.ca.ca_pem
    assert response.not_after > int(time.time())

    # And it works: the certificate just issued admits the connection.
    agents = controller.agent_client(response.certificate_pem)
    with agents, agents.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello())
        assert decode(socket.receive_bytes()).hello_ack.accepted


def test_enrollment_is_closed_when_the_listener_is(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)
    controller.settings.agents.enabled = False
    client = controller.agent_client(None)

    with client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(ENROLL_PATH) as socket:
            socket.send_bytes(wire.Envelope().SerializeToString())
            socket.receive_bytes()


def test_enrollment_refuses_a_first_frame_that_is_not_an_enroll_request(
    controller: Controller,
) -> None:
    client = controller.agent_client(None)
    with client, pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - raises must wrap the close
        with client.websocket_connect(ENROLL_PATH) as socket:
            socket.send_bytes(hello())
            socket.receive_bytes()


# --------------------------------------------------------------------------
# Renewal
# --------------------------------------------------------------------------


def test_a_certificate_in_its_final_third_is_offered_a_renewal(tmp_path: Path) -> None:
    """Over the connection that is already open and already authenticated. No
    cron job, no second channel, no expiry outage."""
    controller = make_controller(tmp_path, auto_approve=True, cert_ttl_days=1)
    certificate = controller.enroll(ENGINE_ID)

    # The operator lengthens the fleet's certificate lifetime. Every existing
    # certificate is now well into its final third, which is the condition
    # this offer exists for -- reached here without faking a clock.
    controller.settings.agents.cert_ttl_days = 90
    controller.context.trust = AgentTrust.from_settings(controller.settings)

    client = controller.agent_client(certificate)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello())
        assert decode(socket.receive_bytes()).hello_ack.accepted
        # The handshake also carries this host's watch list, which is empty
        # here and is sent anyway (`routes/agents.py` says why). Read past it
        # rather than asserting a frame order the protocol does not promise:
        # what this test is about is that the offer arrives at all.
        offer = _await_frame(socket, "renewal_offer").renewal_offer
        assert offer.not_after > int(time.time())

        # The agent answers with a CSR. No token: the connection it arrives on
        # is already mutually authenticated, which is the point of renewing
        # here rather than over a second channel.
        renewed_csr, _ = make_csr()
        socket.send_bytes(
            wire.Envelope(
                certificate_request=wire.CertificateRequest(csr_pem=renewed_csr)
            ).SerializeToString()
        )
        issued = decode(socket.receive_bytes()).certificate_issued

    assert issued.ok
    assert issued.not_after > offer.not_after

    # Both certificates work now. An agent that received the new one but
    # failed to persist it comes back with the old one, and locking that host
    # out would need a person to visit it.
    for pem in (issued.certificate_pem, certificate):
        agents = controller.agent_client(pem)
        with agents, agents.websocket_connect(AGENT_PATH) as socket:
            socket.send_bytes(hello())
            assert decode(socket.receive_bytes()).hello_ack.accepted


def test_a_healthy_certificate_is_not_offered_a_renewal(tmp_path: Path) -> None:
    controller = make_controller(tmp_path, auto_approve=True)
    certificate = controller.enroll(ENGINE_ID)

    client = controller.agent_client(certificate)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello())
        assert decode(socket.receive_bytes()).hello_ack.accepted
        socket.send_bytes(
            wire.Envelope(sync=wire.Sync(slice=wire.SLICE_CONTAINER)).SerializeToString()
        )
        # Nothing between the ack and whatever we send next. If a renewal
        # offer had been queued it would be sitting here.
        assert controller.context.store.snapshot() is not None


def test_a_renewal_takes_its_subject_from_the_certificate_not_the_request(
    controller: Controller,
) -> None:
    """A renewal that trusted the request would be a signing oracle for any
    name an enrolled agent cared to ask for, which is the one thing a CA must
    never be."""
    controller.enroll(ENGINE_ID)
    csr_pem, _ = make_csr(common_name=OTHER_ENGINE)

    outcome = controller.context.trust.renew(ENGINE, csr_pem)

    assert outcome.accepted
    subject = x509.load_pem_x509_certificate(outcome.certificate_pem.encode()).subject
    assert subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == ENGINE


def test_renewal_for_an_unenrolled_host_is_refused(controller: Controller) -> None:
    outcome = controller.context.trust.renew(OTHER_ENGINE, make_csr()[0])

    assert not outcome.accepted


# --------------------------------------------------------------------------
# The registry outlives the process
# --------------------------------------------------------------------------


def test_approval_survives_a_restart(tmp_path: Path) -> None:
    """The graph is ephemeral and rebuildable (ADR-0001); this is not. An
    operator who approved forty hosts last month did not consent to doing it
    again because the process was restarted."""
    controller = make_controller(tmp_path)
    certificate = controller.enroll(ENGINE_ID)
    controller.context.trust.registry.approve(ENGINE)

    restarted = make_controller(tmp_path)
    client = restarted.agent_client(certificate)
    with client, client.websocket_connect(AGENT_PATH) as socket:
        socket.send_bytes(hello())
        assert decode(socket.receive_bytes()).hello_ack.accepted


def test_an_unreadable_registry_is_fatal_rather_than_empty(tmp_path: Path) -> None:
    """The two ways to proceed are "admit nobody", a silent fleet-wide outage,
    and "admit everybody", the thing the file exists to prevent. Neither is a
    thing to do quietly."""
    (tmp_path / "agents.json").write_text("{ this is not the file we wrote")

    with pytest.raises(ValueError, match="unreadable enrollment registry"):
        EnrollmentRegistry.open(tmp_path)


def test_the_registry_is_written_atomically(tmp_path: Path) -> None:
    """A Controller killed mid-write must not come back to a half-written
    allow-list, which `open` treats as fatal -- turning a crash into an
    outage."""
    registry = EnrollmentRegistry.open(tmp_path)
    registry.enroll(ENGINE, serial=1, not_after=0, agent_version="", auto_approve=True)

    assert not list(tmp_path.glob("*.tmp"))
    assert EnrollmentRegistry.open(tmp_path).get(ENGINE).status is AgentStatus.APPROVED


# --------------------------------------------------------------------------
# The operator's surface
# --------------------------------------------------------------------------


def test_an_operator_can_mint_approve_and_revoke(tmp_path: Path) -> None:
    """The workflow ADR-0011 accepts the ceremony of: issue token, install
    agent, approve. Three calls, on the browser-facing port."""
    controller = make_controller(tmp_path)

    with TestClient(controller.ui) as browser:
        minted = browser.post(f"{API_PREFIX}/agents/tokens", json={"ttl_minutes": 15}).json()
        assert minted["ca_fingerprint"] in minted["install"] or minted["token"] in minted["install"]

        outcome = controller.context.trust.enroll(
            join_token=minted["token"],
            engine_id=ENGINE_ID,
            csr_pem=make_csr()[0],
            agent_version="0.1.0",
        )
        assert outcome.accepted

        listed = browser.get(f"{API_PREFIX}/agents").json()
        assert [agent["status"] for agent in listed] == ["pending"]
        assert listed[0]["connected"] is False

        assert browser.post(f"{API_PREFIX}/agents/{ENGINE_ID}/approve").json()["status"] == (
            "approved"
        )
        assert browser.post(f"{API_PREFIX}/agents/{ENGINE_ID}/revoke").json()["status"] == (
            "revoked"
        )
        assert browser.post(f"{API_PREFIX}/agents/nobody/approve").status_code == 404


def test_the_pasted_command_installs_something_that_exists(tmp_path: Path) -> None:
    """The command the dashboard hands out has to be runnable on a bare host.

    It used to be `bystack-agent --controller … --token …`, which assumes a
    binary that is not there -- the button handed you a command you could not
    yet run, and that was step 7's whole claim on the product rather than on
    the release. Both forms are checked because they are different promises:
    one fetches the installer, and one is for a host that already has the
    agent.
    """
    controller = make_controller(tmp_path)

    with TestClient(controller.ui) as browser:
        minted = browser.post(f"{API_PREFIX}/agents/tokens", json={"ttl_minutes": 15}).json()

    for form in (minted["install"], minted["manual"]):
        assert minted["token"] in form
        assert "wss://" in form

    assert "install-agent.sh" in minted["install"]
    # Pinned to a released tag, not to a branch. A `main` URL would mean the
    # command an operator copies changes underneath them between one host and
    # the next.
    assert f"/v{__version__}/" in minted["install"]
    assert minted["manual"].startswith("bystack-agent ")


def test_the_upgrade_command_carries_no_token(tmp_path: Path) -> None:
    """The one difference between installing and upgrading, and it is the load-bearing one.

    A token beside a stored certificate makes the agent enrol a second time, so
    a host upgraded with the install command comes back as a stranger awaiting
    approval while the row an operator was looking at goes quiet. The Controller
    composes the correct line rather than leaving it to be reconstructed from a
    document, so this asserts the absence as much as the contents.
    """
    controller = make_controller(tmp_path)

    with TestClient(controller.ui) as browser:
        terms = browser.get(f"{API_PREFIX}/agents/enrollment").json()
        minted = browser.post(f"{API_PREFIX}/agents/tokens", json={"ttl_minutes": 15}).json()

    upgrade = terms["upgrade"]
    assert "--token" not in upgrade
    assert minted["token"] not in upgrade
    # Otherwise the same command, pinned to this Controller's version, so an
    # upgraded Controller hands out the agent that matches it.
    assert "install-agent.sh" in upgrade
    assert f"/v{__version__}/" in upgrade
    assert "--controller wss://" in upgrade


def test_an_unknown_verdict_never_reaches_the_admitted_branch() -> None:
    """Every `Refusal` has a place in the terminal/retry split. A value added
    later without a decision would be silently treated as retryable, which is
    the wrong default for anything called a refusal."""
    from bystack.api.routes.agents import _TERMINAL

    undecided = set(Refusal) - _TERMINAL
    assert undecided == {Refusal.PENDING, Refusal.CLOCK_SKEW}


def test_a_refused_verdict_reads_as_one_sentence() -> None:
    verdict = Refused(Refusal.CLOCK_SKEW, "900s adrift")
    assert verdict.message == f"{Refusal.CLOCK_SKEW.value}: 900s adrift"


def test_a_certificate_without_a_common_name_is_refused(tmp_path: Path) -> None:
    """Not reachable through our own CA, which always sets one -- which is
    exactly why it is worth asserting. The parser must not hand back an empty
    identity for a certificate some other issuer produced."""
    key = ec.generate_private_key(ec.SECP256R1())
    empty = x509.Name([])
    cert = (
        x509.CertificateBuilder()
        .subject_name(empty)
        .issuer_name(empty)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    from bystack.infra.agentca import read_peer_certificate

    with pytest.raises(CertificateError, match="exactly one common name"):
        read_peer_certificate(cert.public_bytes(serialization.Encoding.PEM).decode())


# --------------------------------------------------------------------------
# The plumbing that carries the certificate
# --------------------------------------------------------------------------


def test_a_scope_without_the_tls_extension_yields_no_certificate() -> None:
    """The fail-closed case, and the one worth being explicit about: uvicorn
    does not populate the ASGI TLS extension, we do, and a release that broke
    that must strand every agent rather than admit every caller."""
    assert peer_certificate_pem({}) is None
    assert peer_certificate_pem({"extensions": {}}) is None
    assert peer_certificate_pem({"extensions": {"tls": {"client_cert_chain": []}}}) is None


def test_the_leaf_certificate_is_carried_as_pem(controller: Controller) -> None:
    """DER on the socket, PEM in the scope, and the same certificate either
    way -- asserted by parsing it back rather than by comparing strings."""
    certificate = controller.enroll(ENGINE_ID)
    der = x509.load_pem_x509_certificate(certificate.encode()).public_bytes(
        serialization.Encoding.DER
    )

    extension = _tls_extension(_FakeTransport(der))
    assert extension is not None
    recovered = peer_certificate_pem({"extensions": {"tls": extension}})
    assert recovered is not None
    assert x509.load_pem_x509_certificate(recovered.encode()).subject.get_attributes_for_oid(
        NameOID.COMMON_NAME
    )[0].value == ENGINE


def test_a_plain_tcp_connection_has_no_tls_extension() -> None:
    assert _tls_extension(_FakeTransport(None, tls=False)) is None


class _FakeTransport:
    """Only what `_tls_extension` reads: `get_extra_info("ssl_object")`."""

    def __init__(self, peer_der: bytes | None, *, tls: bool = True) -> None:
        self._ssl = _FakeSSLObject(peer_der) if tls else None

    def get_extra_info(self, name: str) -> object:
        return self._ssl if name == "ssl_object" else None


class _FakeSSLObject:
    def __init__(self, peer_der: bytes | None) -> None:
        self._peer = peer_der

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        assert binary_form, "the parsed form is not what the identity is read from"
        return self._peer

    def version(self) -> str:
        return "TLSv1.3"

    def cipher(self) -> tuple[str, str, int]:
        return ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
