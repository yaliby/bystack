"""Test fixtures.

Nothing here touches Docker or the network. If a test in this suite ever
needs a daemon, an abstraction has leaked.

What touches disk is the Controller's durable state: the CA key and the
enrollment registry (ADR-0011), and the audit log (ADR-0012). All three are
files in `tmp_path`, created and thrown away per test, and the certificates
are real -- generated, signed and parsed by the same code the Controller
runs. A fake CA would test the fake.

`_state_dir_is_disposable` is what makes that true for tests that never
mention a path. It is autouse; read its docstring before removing it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bystack import config
from bystack.api.app import (
    build_context,
    create_agent_app,
    create_app,
    create_local_agent_app,
)
from bystack.api.deps import AppContext
from bystack.config import AgentsConfig, LocalAgentConfig, Settings

#: Exercises the pre-25.0 colon-delimited engine ID format end to end, so
#: the normalization is covered by every mapper and ingest test, not just the
#: one that names it.
ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE_ID_SAFE = "AAAABBBBCCCC"


# --------------------------------------------------------------------------
# Agent trust (ADR-0011)
# --------------------------------------------------------------------------


class WithClientCertificate:
    """Publish a client certificate into the ASGI scope, as TLS would.

    `bystack.api.tls` puts the peer's certificate in `scope["extensions"]
    ["tls"]` -- the standard ASGI TLS extension -- precisely so that the thing
    supplying it can be swapped. In production that is a uvicorn protocol
    subclass reading the socket; here it is four lines, and the route cannot
    tell the difference because it only ever reads the standard shape.

    Passing ``None`` is not "skip the middleware": it is the *unauthenticated*
    case, and it has tests of its own. Every refusal path in ADR-0011 is
    reachable from here without a listener, a port, or a handshake.
    """

    def __init__(self, app: FastAPI, certificate_pem: str | None) -> None:
        self._app = app
        self._pem = certificate_pem

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "websocket" and self._pem is not None:
            scope.setdefault("extensions", {})["tls"] = {"client_cert_chain": [self._pem]}
        await self._app(scope, receive, send)


def make_csr(common_name: str = "unused") -> tuple[str, ec.EllipticCurvePrivateKey]:
    """A real PKCS#10 request. The name in it is deliberately ignored.

    The CA sets the subject from the engine id it decided to believe, never
    from the CSR, so this fixture defaults to a name that would be wrong if it
    were ever used -- and a test that asserts the issued subject would catch
    the day it is.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode(), key


@dataclass(slots=True)
class Controller:
    """Both listeners over one object graph, which is how the process runs it.

    The operator mints a token on the browser app and the agent redeems it on
    the other one. A fixture that gave each its own context would let a test
    pass while the two halves disagreed about who is enrolled.
    """

    settings: Settings
    context: AppContext
    ui: FastAPI
    agents: FastAPI
    local: FastAPI
    """The unix-socket listener, as a third app over the same context.

    It is here rather than in one test file because the interesting assertions
    are comparisons: the same frames, with no certificate, are admitted on
    this app and refused on `agents`. A fixture that could only build one of
    them could not state that.
    """

    def agent_client(self, certificate_pem: str | None = None) -> TestClient:
        return TestClient(WithClientCertificate(self.agents, certificate_pem))

    def local_client(self) -> TestClient:
        """No certificate, ever. There is no way to present one here."""
        return TestClient(self.local)

    def enroll(self, engine_id: str, *, csr_pem: str | None = None) -> str:
        """Mint a token, redeem it, return the certificate. The happy path.

        Used by tests that are about something else and need an admitted
        agent to get there. The tests that are about enrollment itself call
        `context.trust` directly, one step at a time.
        """
        token = self.context.trust.mint_token(dt.timedelta(minutes=15))
        outcome = self.context.trust.enroll(
            join_token=token.token,
            engine_id=engine_id,
            csr_pem=csr_pem or make_csr()[0],
            agent_version="0.1.0",
        )
        assert outcome.accepted, outcome.reason
        return outcome.certificate_pem


@pytest.fixture(autouse=True)
def _state_dir_is_disposable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test writes outside its own temporary directory. Ever.

    Autouse and unconditional, because the tests this catches are the ones
    that never mention a path: several build an app from a bare ``Settings()``
    to exercise a route, and every one of those would otherwise mint a CA and
    append to an audit log in the home directory of whoever ran the suite —
    silently, and cumulatively, so a run's results depend on every run before
    it.

    `make_controller` passes `tmp_path` explicitly and does not need this. It
    is here for everything that does not.
    """
    monkeypatch.setattr(config, "DEFAULT_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def controller(tmp_path: Path) -> Controller:
    return make_controller(tmp_path)


def make_controller(
    state_dir: Path, *, read_only: bool = True, local_agent: bool = False, **agents: Any
) -> Controller:
    # `read_only` is a Settings field rather than an agents one, and it has to
    # be right *here*: `CommandService` captures it when the context is built,
    # so a test that flips `settings.read_only` afterwards is testing the
    # default it thought it had overridden.
    #
    # The local agent is off unless a test asks, and off is not the product
    # default. Nothing here spawns a child -- `LocalAgent.start` is called by
    # `main.serve` and by nothing else -- but leaving it enabled would make
    # every fixture's reported state depend on whether the machine running the
    # suite happens to have a Docker socket.
    settings = Settings(
        agents=AgentsConfig(enabled=True, state_dir=str(state_dir), **agents),
        local_agent=LocalAgentConfig(enabled=local_agent),
        read_only=read_only,
    )
    context = build_context(settings)
    return Controller(
        settings,
        context,
        create_app(settings, context),
        create_agent_app(context),
        create_local_agent_app(context),
    )


@pytest.fixture
def engine_info() -> dict[str, Any]:
    return {
        "ID": ENGINE_ID,
        "Name": "lab-node-01",
        "ServerVersion": "29.6.0",
        "OperatingSystem": "Fedora Linux 44",
        "KernelVersion": "6.19.10",
        "Architecture": "x86_64",
        "NCPU": 8,
        "MemTotal": 16_000_000_000,
        "ContainersRunning": 2,
        "Containers": 3,
    }


def make_container(
    container_id: str,
    name: str,
    *,
    state: str = "running",
    project: str | None = None,
    service: str | None = None,
    depends_on: str | None = None,
    network_id: str = "net1",
    volume: str | None = None,
    image_id: str = "sha256:abc123",
) -> dict[str, Any]:
    labels: dict[str, str] = {}
    if project and service:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service
        labels["com.docker.compose.container-number"] = "1"
    if depends_on:
        labels["com.docker.compose.depends_on"] = depends_on

    mounts: list[dict[str, Any]] = [
        # A bind mount: a real dependency, but not an engine-managed entity.
        # Present in every fixture so the mapper's refusal to invent a node
        # for it stays covered.
        {"Type": "bind", "Source": "/etc/localtime", "Destination": "/etc/localtime"}
    ]
    if volume:
        mounts.append(
            {"Type": "volume", "Name": volume, "Destination": "/data", "Mode": "rw", "RW": True}
        )

    return {
        "Id": container_id,
        "Names": [f"/{name}"],
        "Image": "nginx:latest",
        "ImageID": image_id,
        "Command": "nginx -g daemon off;",
        "Created": 1_700_000_000,
        "State": state,
        "Status": "Up 3 hours",
        "Labels": labels,
        "Ports": [
            {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"},
            {"PrivatePort": 443, "Type": "tcp"},  # unpublished -- must be dropped
        ],
        "Mounts": mounts,
        "NetworkSettings": {
            "Networks": {
                "bridge": {"NetworkID": network_id, "IPAddress": "172.17.0.2", "Aliases": []}
            }
        },
    }


@pytest.fixture
def container() -> dict[str, Any]:
    return make_container("c" * 64, "web", project="shop", service="web", volume="shop_data")


@pytest.fixture
def network() -> dict[str, Any]:
    return {
        "Id": "net1",
        "Name": "shop_default",
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]},
    }


@pytest.fixture
def volume() -> dict[str, Any]:
    return {
        "Name": "shop_data",
        "Driver": "local",
        "Mountpoint": "/var/lib/docker/volumes/shop_data/_data",
        "Scope": "local",
        "Labels": {},
        "CreatedAt": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def image() -> dict[str, Any]:
    return {
        "Id": "sha256:abc123",
        "RepoTags": ["nginx:latest"],
        "RepoDigests": ["nginx@sha256:def456"],
        "Size": 142_000_000,
        "Created": 1_699_000_000,
        "Labels": {},
    }
