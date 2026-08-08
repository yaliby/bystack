"""The Controller's internal CA.

One key, one purpose: issuing and rotating agent client certificates, plus the
server certificate for the listener those agents dial. It is deliberately not
a general-purpose PKI -- it signs two shapes of certificate and nothing else,
and every field in them is set here rather than copied from a request.

**Key custody is now a thing this project has.** ADR-0011 records the cost
plainly: the CA is small, but it is a thing that can be lost, and losing it
means re-enrolling the fleet. The mitigation is that it lives in one directory
with one obvious name, so it is a thing an operator can back up.

Nothing here reads configuration or talks to the network. It is handed a
directory and produces certificates, which is what makes it testable without a
listener, a socket, or a clock that anyone had to fake.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPublicKeyTypes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: The CA outlives the certificates it signs by a wide margin, because
#: replacing it is the expensive operation (re-enrol every host) and replacing
#: a leaf is the cheap one (it happens by itself over the open stream).
CA_LIFETIME = dt.timedelta(days=3650)

#: The listener's own certificate. Short like an agent's, and reissued
#: automatically whenever it is close to expiry, because unlike an agent's it
#: needs no approval from anyone.
SERVER_LIFETIME = dt.timedelta(days=90)

#: Backdating on every certificate we issue.
#:
#: Not a security decision -- it is the difference between "your agent works"
#: and "your agent works in thirty seconds" on a fleet whose clocks disagree
#: by a little. Clocks that disagree by a *lot* are a separate failure with
#: its own diagnosis; see `Hello.unix_time`.
CLOCK_SLACK = dt.timedelta(minutes=5)

#: Refuse a key we would not want protecting a root-equivalent connection.
MIN_RSA_BITS = 2048

_CA_KEY = "agent-ca.key"
_CA_CERT = "agent-ca.crt"
_SERVER_KEY = "controller.key"
_SERVER_CERT = "controller.crt"


class CertificateError(ValueError):
    """A CSR we will not sign, or a certificate we will not accept."""


@dataclass(frozen=True, slots=True)
class Issued:
    """A freshly signed certificate, with the facts a caller needs."""

    certificate_pem: str
    serial: int
    not_after: dt.datetime

    @property
    def not_after_unix(self) -> int:
        return int(self.not_after.timestamp())


@dataclass(frozen=True, slots=True)
class ServerCredentials:
    """Paths, because that is what an SSL context wants."""

    certificate: Path
    key: Path


class AgentCA:
    """Issue, renew and identify. Revocation is the registry's job.

    There is no CRL and there will not be one. Revocation is an allow-list
    check against the enrolled-agent registry at connection time: with an
    authoritative list of every agent we ever issued to, CRL machinery would
    be ceremony around a lookup we already have to do (ADR-0011).
    """

    __slots__ = ("_dir", "_key", "_cert")

    def __init__(self, directory: Path, key: ec.EllipticCurvePrivateKey, cert: x509.Certificate):
        self._dir = directory
        self._key = key
        self._cert = cert

    # -- custody ----------------------------------------------------------

    @classmethod
    def open(cls, directory: str | os.PathLike[str]) -> AgentCA:
        """Load the CA from ``directory``, creating it on first run.

        Creating on first run rather than making the operator run a setup
        command is the same judgement as everywhere else in this project: the
        first-run experience is the product, and a control plane that refuses
        to start until you have generated a key is one more page of
        documentation between a person and a working graph.
        """
        path = Path(directory).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        # 0700 on the directory as well as 0600 on the key: the key is the
        # fleet's trust root, and a mode set only on the file is one `umask`
        # away from a world-readable parent listing it.
        os.chmod(path, 0o700)

        key_path, cert_path = path / _CA_KEY, path / _CA_CERT
        if key_path.exists() and cert_path.exists():
            key = _load_ec_key(key_path)
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            return cls(path, key, cert)

        key = ec.generate_private_key(ec.SECP384R1())
        now = _now()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ByStack Agent CA")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SLACK)
            .not_valid_after(now + CA_LIFETIME)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .sign(key, hashes.SHA256())
        )
        _write_secret(key_path, key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cls(path, key, cert)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def ca_pem(self) -> str:
        return self._cert.public_bytes(serialization.Encoding.PEM).decode()

    @property
    def ca_certificate_path(self) -> Path:
        return self._dir / _CA_CERT

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the CA certificate, hex.

        This is what a join token carries, and it is what turns enrollment
        from trust-on-first-use into an authenticated exchange: the agent has
        the fingerprint before it has anything else, so it can refuse to send
        its token to a server whose chain does not end here.
        """
        return hashlib.sha256(self._cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    # -- issuing ----------------------------------------------------------

    def issue_agent_certificate(self, csr_pem: str, engine_id: str, ttl_days: int) -> Issued:
        """Sign a client certificate binding ``engine_id`` into the subject.

        The subject comes from ``engine_id`` -- which the caller has already
        decided it believes -- and never from the CSR. A CSR is an untrusted
        document with a signature proving only that its sender holds the
        matching private key; treating any name inside it as an assertion
        about identity is the classic way a CA issues a certificate for
        someone else's name.
        """
        csr = _load_csr(csr_pem)
        public_key = _acceptable_public_key(csr)

        now = _now()
        not_after = now + dt.timedelta(days=ttl_days)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, engine_id)]))
            .issuer_name(self._cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SLACK)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            # clientAuth only. An agent certificate that could also serve TLS
            # is a credential for standing up something that looks like a
            # Controller, and nothing about this design needs it.
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), False
            )
            .sign(self._key, hashes.SHA256())
        )
        return Issued(
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
            serial=cert.serial_number,
            not_after=not_after,
        )

    def server_credentials(self, subject_names: list[str]) -> ServerCredentials:
        """The listener's certificate, issued and reissued as needed.

        Reissued rather than rotated on a schedule: this certificate has no
        approval step and no operator in the loop, so the cheapest correct
        thing is to check it every time the Controller starts and replace it
        when it is stale or when the configured names have changed.
        """
        cert_path, key_path = self._dir / _SERVER_CERT, self._dir / _SERVER_KEY
        wanted = _san(subject_names)

        if cert_path.exists() and key_path.exists():
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            fresh = existing.not_valid_after_utc > _now() + SERVER_LIFETIME / 3
            names_match = _san_of(existing) == _names_of(wanted)
            issued_by_us = existing.issuer == self._cert.subject
            if fresh and names_match and issued_by_us:
                return ServerCredentials(cert_path, key_path)

        key = ec.generate_private_key(ec.SECP256R1())
        now = _now()
        cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ByStack Controller")])
            )
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SLACK)
            .not_valid_after(now + SERVER_LIFETIME)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(wanted), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), False
            )
            .sign(self._key, hashes.SHA256())
        )
        _write_secret(key_path, key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        # The chain, not just the leaf: an agent enrolling has only a
        # fingerprint to check, and it can only check it against a CA
        # certificate the handshake actually gave it.
        cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + self._cert.public_bytes(serialization.Encoding.PEM)
        )
        return ServerCredentials(cert_path, key_path)


# --------------------------------------------------------------------------
# Reading what an agent presents
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    """What the TLS layer proved about the agent on the other end."""

    engine_id: str
    serial: int
    not_after: dt.datetime


def read_peer_certificate(pem: str) -> PeerIdentity:
    """Extract the identity from a client certificate the TLS layer accepted.

    This function does **not** validate the chain, and must not be called on a
    certificate that was not already verified against our CA by the TLS
    handshake. Splitting it that way is deliberate: chain validation belongs
    in one place, and a helper that sometimes verifies and sometimes does not
    is how the not-verifying path ends up on a live connection.
    """
    try:
        cert = x509.load_pem_x509_certificate(pem.encode())
    except ValueError as exc:
        raise CertificateError(f"unreadable client certificate: {exc}") from exc

    common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1:
        raise CertificateError("client certificate does not carry exactly one common name")
    engine_id = str(common_names[0].value)
    if not engine_id:
        raise CertificateError("client certificate carries an empty common name")

    return PeerIdentity(
        engine_id=engine_id, serial=cert.serial_number, not_after=cert.not_valid_after_utc
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write_secret(path: Path, data: bytes) -> None:
    """Write with the mode set before any bytes exist.

    `open()` then `chmod()` leaves a window in which the private key is on
    disk under the default umask. It is a small window and it is on the one
    file in this project where it is least acceptable.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb") as fh:
        fh.write(data)


def _load_ec_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise CertificateError(f"{path} is not the EC key this CA was created with")
    return key


def _load_csr(csr_pem: str) -> x509.CertificateSigningRequest:
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as exc:
        raise CertificateError(f"unreadable certificate request: {exc}") from exc
    # Proves the sender holds the private key for the public key inside. It is
    # the *only* thing a CSR proves, which is why nothing else in it is used.
    if not csr.is_signature_valid:
        raise CertificateError("certificate request is not signed by its own key")
    return csr


def _acceptable_public_key(
    csr: x509.CertificateSigningRequest,
) -> CertificateIssuerPublicKeyTypes:
    """Refuse a key we would not want on a root-equivalent connection.

    An allow-list rather than a size check, because "not obviously too small"
    is not the same question as "something we are prepared to trust for ninety
    days".
    """
    key = csr.public_key()
    if isinstance(key, ed25519.Ed25519PublicKey):
        return key
    if isinstance(key, ec.EllipticCurvePublicKey):
        if isinstance(key.curve, ec.SECP256R1 | ec.SECP384R1 | ec.SECP521R1):
            return key
        raise CertificateError(f"unsupported curve {key.curve.name}")
    if isinstance(key, rsa.RSAPublicKey):
        if key.key_size < MIN_RSA_BITS:
            raise CertificateError(f"RSA key is {key.key_size} bits, minimum {MIN_RSA_BITS}")
        return key
    raise CertificateError(f"unsupported key type {type(key).__name__}")


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    """Turn configured names into SAN entries, guessing nothing.

    ``localhost`` is a DNS name and ``127.0.0.1`` is an address, and a
    certificate that puts one in the other's slot fails verification in a way
    that reads as "the CA is broken".
    """
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _san_of(cert: x509.Certificate) -> set[tuple[str, str]]:
    try:
        extension = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return set()
    return _names_of(extension.value)


def _names_of(san: x509.SubjectAlternativeName) -> set[tuple[str, str]]:
    """Kind and value, so a DNS entry never compares equal to an address."""
    return {(type(entry).__name__, str(entry.value)) for entry in san}
