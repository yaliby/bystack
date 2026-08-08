"""The Controller's agent trust machinery (ADR-0011).

Three pieces, deliberately separate:

- :mod:`ca` issues certificates and knows nothing about who should have one.
- :mod:`tokens` mints and burns the bootstrap credential and knows nothing
  about certificates.
- :mod:`registry` decides who is allowed to connect and knows nothing about
  either -- it holds statuses and serials.

The seams are where they are because each of the three has a different
lifetime and a different custody problem, and because the interesting failure
is a check that lives in two places and is updated in one.
"""

from bystack.infra.agentca.ca import (
    AgentCA,
    CertificateError,
    Issued,
    PeerIdentity,
    ServerCredentials,
    read_peer_certificate,
)
from bystack.infra.agentca.registry import (
    Admission,
    AgentStatus,
    EnrolledAgent,
    EnrollmentRegistry,
)
from bystack.infra.agentca.tokens import JoinTokenStore, MintedToken, Outcome

__all__ = [
    "Admission",
    "AgentCA",
    "AgentStatus",
    "CertificateError",
    "EnrolledAgent",
    "EnrollmentRegistry",
    "Issued",
    "JoinTokenStore",
    "MintedToken",
    "Outcome",
    "PeerIdentity",
    "ServerCredentials",
    "read_peer_certificate",
]
