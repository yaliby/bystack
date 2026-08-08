"""Agent trust, as one decision surface.

The CA issues, the token store burns, the registry remembers. This is the
object that puts them in order and applies policy to the result -- lifetimes,
auto-approval, how much clock disagreement is too much.

It exists so that every entry point asks the same question of the same object.
The enrollment path, the connection path and the renewal path each have a way
to be subtly wrong on their own, and a check that lives in three places is a
check that gets updated in two (ADR-0011's closing note: authentication code
tested only on the success path is not tested).

Nothing here knows what a WebSocket or a protobuf frame is. The route builds
frames from these results, which is what lets every refusal path be tested
without a socket.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum

from bystack.config import Settings
from bystack.core.identity import engine_scope
from bystack.infra.agentca import (
    Admission,
    AgentCA,
    CertificateError,
    EnrollmentRegistry,
    JoinTokenStore,
    MintedToken,
    Outcome,
    read_peer_certificate,
)

log = logging.getLogger(__name__)

#: Renewal is offered once a certificate is two thirds through its life.
#:
#: Late enough that a healthy agent renews rarely, early enough that the
#: remaining third is many reconnections' worth of opportunity. An agent that
#: is offline for the whole window has a bigger problem than its certificate.
RENEW_AFTER = 2 / 3

#: A ceiling on what an unauthenticated caller can make us parse.
#:
#: The enrollment endpoint is the one place in the Controller that reads
#: attacker-supplied input before any identity has been established, so the
#: bound is here rather than only on the frame.
MAX_CSR_BYTES = 16 * 1024


class Refusal(Enum):
    """Why a connection was not allowed to carry observations.

    Every one of these has a test asserting the connection is *refused*, which
    is the requirement ADR-0011 ends on.
    """

    NO_CERTIFICATE = "no client certificate was presented"
    UNREADABLE_CERTIFICATE = "the client certificate could not be read"
    IDENTITY_MISMATCH = "the certificate subject is not the engine id in Hello"
    UNKNOWN = "this agent is not enrolled"
    PENDING = "this agent is enrolled but not approved"
    REVOKED = "this agent's certificate has been revoked"
    SUPERSEDED = "this certificate was superseded by a later enrollment"
    CLOCK_SKEW = "this host's clock disagrees with the Controller's"


@dataclass(frozen=True, slots=True)
class Admitted:
    """The connection may proceed, as this host."""

    engine_id: str
    serial: int
    not_after: dt.datetime
    #: Set when the certificate is far enough through its life to renew.
    renew: bool
    #: Admitted over the local socket rather than by certificate.
    #:
    #: Carried through so that everything downstream -- the fleet list, the
    #: renewal offer, the audit of who is managing what -- can tell the two
    #: apart. A host that was never enrolled must not be shown as approved, and
    #: an agent with no certificate must never be offered a renewal.
    local: bool = False


@dataclass(frozen=True, slots=True)
class Refused:
    reason: Refusal
    detail: str = ""

    @property
    def message(self) -> str:
        return f"{self.reason.value}{f': {self.detail}' if self.detail else ''}"


Verdict = Admitted | Refused


@dataclass(frozen=True, slots=True)
class Enrollment:
    """The outcome of redeeming a token."""

    accepted: bool
    reason: str = ""
    certificate_pem: str = ""
    ca_pem: str = ""
    not_after: int = 0
    pending_approval: bool = False


class AgentTrust:
    """Enrollment, admission and renewal for the whole fleet."""

    __slots__ = ("_ca", "_tokens", "_registry", "_ttl_days", "_auto_approve", "_max_skew")

    def __init__(
        self,
        ca: AgentCA,
        tokens: JoinTokenStore,
        registry: EnrollmentRegistry,
        *,
        cert_ttl_days: int,
        auto_approve: bool,
        max_clock_skew: int,
    ) -> None:
        self._ca = ca
        self._tokens = tokens
        self._registry = registry
        self._ttl_days = cert_ttl_days
        self._auto_approve = auto_approve
        self._max_skew = max_clock_skew

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentTrust:
        """Open the CA and the registry, creating them on first run.

        Called whether or not the listener is enabled, because the operator
        routes -- mint a token, list agents, approve one -- are on the browser
        port and are how an operator turns the listener on in the first place.
        """
        ca = AgentCA.open(settings.agents.state_dir)
        return cls(
            ca,
            JoinTokenStore(ca.fingerprint),
            EnrollmentRegistry.open(settings.agents.state_dir),
            cert_ttl_days=settings.agents.cert_ttl_days,
            auto_approve=settings.agents.auto_approve,
            max_clock_skew=settings.agents.max_clock_skew,
        )

    @property
    def ca(self) -> AgentCA:
        return self._ca

    @property
    def registry(self) -> EnrollmentRegistry:
        return self._registry

    # -- operator ---------------------------------------------------------

    def mint_token(self, ttl: dt.timedelta) -> MintedToken:
        return self._tokens.mint(ttl)

    # -- enrollment -------------------------------------------------------

    def enroll(
        self, *, join_token: str, engine_id: str, csr_pem: str, agent_version: str
    ) -> Enrollment:
        """Redeem a token for one certificate.

        The order matters. The token is checked before the CSR is parsed, so
        an unauthenticated caller cannot make us do asymmetric cryptography on
        a document of their choosing; and it is burned only once we have a
        certificate to hand back, so a malformed CSR costs a retry rather than
        a second trip to the host.
        """
        if not engine_id:
            return Enrollment(False, "enrollment carried no engine id")
        if len(csr_pem) > MAX_CSR_BYTES:
            return Enrollment(False, "certificate request is implausibly large")

        scoped = engine_scope(engine_id)

        # Checked, not yet burned: see the docstring. Redemption is the last
        # thing that happens on the success path.
        outcome = self._tokens.redeem(join_token)
        if outcome is not Outcome.REDEEMED:
            log.warning("enrollment refused for %s: token %s", scoped, outcome.value)
            return Enrollment(False, f"join token {outcome.value}")

        try:
            issued = self._ca.issue_agent_certificate(csr_pem, scoped, self._ttl_days)
        except CertificateError as exc:
            log.warning("enrollment refused for %s: %s", scoped, exc)
            return Enrollment(False, str(exc))

        agent = self._registry.enroll(
            scoped,
            serial=issued.serial,
            not_after=issued.not_after_unix,
            agent_version=agent_version,
            auto_approve=self._auto_approve,
        )
        pending = agent.status.value != "approved"
        log.info(
            "enrolled agent %s (%s), certificate valid until %s",
            scoped, agent.status.value, issued.not_after.isoformat(timespec="seconds"),
        )
        return Enrollment(
            accepted=True,
            certificate_pem=issued.certificate_pem,
            ca_pem=self._ca.ca_pem,
            not_after=issued.not_after_unix,
            pending_approval=pending,
        )

    # -- connection -------------------------------------------------------

    def admit(
        self,
        *,
        client_certificate_pem: str | None,
        hello_engine_id: str,
        agent_unix_time: int,
        agent_version: str = "",
    ) -> Verdict:
        """The gate every agent connection passes through.

        Fails closed on every path, including the one where the transport
        never told us about a certificate at all: an agent listener that
        cannot see the peer's certificate has no way to know who is calling,
        and answering "probably fine" there would make the whole of ADR-0011
        decorative.
        """
        if not client_certificate_pem:
            return Refused(Refusal.NO_CERTIFICATE)

        try:
            peer = read_peer_certificate(client_certificate_pem)
        except CertificateError as exc:
            return Refused(Refusal.UNREADABLE_CERTIFICATE, str(exc))

        # "Which host is this" and "which agent is this" are the same question
        # (ADR-0002, ADR-0011), so an agent claiming an engine id its
        # certificate does not carry is claiming to be a host it is not.
        claimed = engine_scope(hello_engine_id)
        if claimed != peer.engine_id:
            return Refused(
                Refusal.IDENTITY_MISMATCH,
                f"certificate says {peer.engine_id}, Hello says {claimed}",
            )

        skew = self._skew(agent_unix_time)
        if skew is not None:
            return Refused(Refusal.CLOCK_SKEW, skew)

        admission = self._registry.admit(peer.engine_id, peer.serial)
        if admission is not Admission.ADMITTED:
            return Refused(_REFUSALS[admission], admission.value)

        # The version comes from the connection, not from the enrollment. An
        # agent upgraded in place never enrols again, so a record written once
        # at join time would report its original version for the life of the
        # host.
        self._registry.seen(peer.engine_id, agent_version=agent_version)
        return Admitted(
            engine_id=peer.engine_id,
            serial=peer.serial,
            not_after=peer.not_after,
            renew=self._due_for_renewal(peer.not_after),
        )

    def admit_local(self, hello_engine_id: str) -> Verdict:
        """The gate for the agent this Controller spawned itself.

        There is no certificate to check because there is nothing for one to
        establish. This connection arrived on a unix socket in a directory only
        this user can enter, from a process this Controller forked; a peer able
        to open it can already read the Docker socket the agent is reading, so
        a CA round trip between a parent and its own child would be ceremony
        rather than a control.

        What is *not* skipped is the engine id: it still scopes the partition,
        and a local agent with no id has nowhere to write. Everything above
        this point -- ingest, the writer, partition isolation -- treats it
        exactly like any other host, which is the point of routing the local
        case through the same code (`docs/MIGRATION.md` §4).

        It is deliberately not registered as an enrolled agent. Enrollment
        records operator intent about a *remote* host and is the one durable
        thing the Controller keeps (ADR-0001); a local agent is a consequence
        of configuration, is re-derived on every start, and would otherwise
        accumulate a row per machine the Controller was ever run on.
        """
        if not hello_engine_id:
            return Refused(Refusal.UNKNOWN, "the local agent reported no engine id")
        return Admitted(
            engine_id=engine_scope(hello_engine_id),
            serial=0,
            not_after=dt.datetime.now(dt.UTC),
            renew=False,
            local=True,
        )

    def renew(self, engine_id: str, csr_pem: str) -> Enrollment:
        """Issue over a connection that is already mutually authenticated.

        No token, because there is nothing left for one to prove: the caller
        holds a certificate we issued, for this engine id, on a connection the
        TLS layer verified. Requiring a second credential here would put a
        human back in the loop every ninety days, which is the outage this
        design exists to avoid.
        """
        if len(csr_pem) > MAX_CSR_BYTES:
            return Enrollment(False, "certificate request is implausibly large")
        agent = self._registry.get(engine_id)
        if agent is None:
            return Enrollment(False, "this agent is not enrolled")

        try:
            issued = self._ca.issue_agent_certificate(csr_pem, engine_id, self._ttl_days)
        except CertificateError as exc:
            return Enrollment(False, str(exc))

        self._registry.renewed(engine_id, serial=issued.serial, not_after=issued.not_after_unix)
        log.info(
            "renewed agent %s until %s",
            engine_id, issued.not_after.isoformat(timespec="seconds"),
        )
        return Enrollment(
            accepted=True,
            certificate_pem=issued.certificate_pem,
            ca_pem=self._ca.ca_pem,
            not_after=issued.not_after_unix,
        )

    # -- internals --------------------------------------------------------

    def _skew(self, agent_unix_time: int) -> str | None:
        """A message when the agent's clock is too far off, otherwise None.

        Zero means an agent too old to report its clock. Refusing those would
        make a field addition a fleet-wide outage, and mixed-version fleets
        are a normal operating state (ADR-0008) -- so an unreported clock is
        left to the TLS layer, which is where it was diagnosed before.
        """
        if agent_unix_time == 0:
            return None
        drift = abs(int(dt.datetime.now(dt.UTC).timestamp()) - agent_unix_time)
        if drift <= self._max_skew:
            return None
        return (
            f"{drift}s adrift, tolerance {self._max_skew}s. "
            "Certificate validation is time-sensitive; fix the host's clock (NTP)."
        )

    def _due_for_renewal(self, not_after: dt.datetime) -> bool:
        remaining = not_after - dt.datetime.now(dt.UTC)
        return remaining < dt.timedelta(days=self._ttl_days) * (1 - RENEW_AFTER)


_REFUSALS = {
    Admission.UNKNOWN: Refusal.UNKNOWN,
    Admission.PENDING: Refusal.PENDING,
    Admission.REVOKED: Refusal.REVOKED,
    Admission.SUPERSEDED: Refusal.SUPERSEDED,
}
