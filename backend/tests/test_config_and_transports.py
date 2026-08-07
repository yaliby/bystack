"""Configuration loading and the transport abstraction."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from bystack.config import Settings
from bystack.core.ports.transport import TransportError, TransportState
from bystack.infra.transports.local_socket import LocalSocketConfig, LocalSocketTransport
from bystack.infra.transports.registry import build_transport
from bystack.infra.transports.ssh_tunnel import SSHTunnelConfig, SSHTunnelTransport


def test_default_settings_manage_the_local_engine() -> None:
    settings = Settings.default()

    assert [h.id for h in settings.hosts] == ["local"]
    assert settings.hosts[0].transport.type == "local_socket"


def test_secure_defaults() -> None:
    settings = Settings.default()

    # Mutation is opt-in.
    assert settings.read_only is True
    # This service holds root-equivalent access to every managed engine;
    # binding it to the world on first run is not a shippable default.
    assert settings.api.host == "127.0.0.1"


def test_config_selects_a_transport_by_type(tmp_path: Path) -> None:
    config = tmp_path / "bystack.yaml"
    config.write_text(
        """
hosts:
  - id: lab-01
    transport:
      type: ssh_tunnel
      host: 10.0.0.5
      username: ops
"""
    )

    settings = Settings.load(config)

    assert isinstance(settings.hosts[0].transport, SSHTunnelConfig)
    assert settings.hosts[0].transport.host == "10.0.0.5"


def test_unknown_transport_type_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "bystack.yaml"
    config.write_text("hosts:\n  - id: x\n    transport:\n      type: carrier_pigeon\n")

    with pytest.raises(ValueError):
        Settings.load(config)


def test_malformed_config_fails_loudly(tmp_path: Path) -> None:
    # A typo that quietly degraded to "manage nothing" would look exactly
    # like an empty cluster, and the user would have no way to tell.
    config = tmp_path / "bystack.yaml"
    config.write_text("hosts:\n  - transport:\n      type: local_socket\n")  # missing id

    with pytest.raises(ValueError):
        Settings.load(config)


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Settings.load(tmp_path / "nope.yaml")


def test_registry_builds_each_known_transport() -> None:
    assert isinstance(
        build_transport("t", LocalSocketConfig(path="/tmp/x.sock")), LocalSocketTransport
    )
    assert isinstance(build_transport("t", SSHTunnelConfig(host="h")), SSHTunnelTransport)


async def test_local_socket_transport_reports_a_real_socket(tmp_path: Path) -> None:
    path = str(tmp_path / "engine.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        transport = LocalSocketTransport("local", LocalSocketConfig(path=path))
        endpoint = await transport.open()

        assert endpoint.uds_path == path
        assert transport.health().state is TransportState.OPEN
    finally:
        server.close()


async def test_local_socket_transport_explains_a_missing_socket(tmp_path: Path) -> None:
    transport = LocalSocketTransport("local", LocalSocketConfig(path=str(tmp_path / "no.sock")))

    with pytest.raises(TransportError, match="does not exist"):
        await transport.open()

    assert transport.health().state is TransportState.FAILED


async def test_local_socket_transport_rejects_a_non_socket(tmp_path: Path) -> None:
    regular = tmp_path / "not-a-socket"
    regular.write_text("")
    transport = LocalSocketTransport("local", LocalSocketConfig(path=str(regular)))

    with pytest.raises(TransportError, match="not a socket"):
        await transport.open()


def test_ssh_host_key_verification_is_on_by_default() -> None:
    # asyncssh's sentinels are inverted from intuition (None disables
    # verification), so the default is asserted explicitly rather than trusted.
    assert SSHTunnelConfig(host="h").insecure_skip_host_key_check is False
