"""SSH tunnel transport -- the default for remote hosts.

Forwards a local Unix socket to the remote engine socket, so the Docker
provider sees exactly what it sees for a local engine and remains entirely
unaware that SSH is involved.

Chosen as the remote default over Docker-over-TCP because it requires no PKI
to manage and, more importantly, requires no Docker daemon to be exposed on
the network. The Docker socket is root-equivalent on the host; not publishing
it is worth a great deal.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import Literal

import asyncssh
from pydantic import BaseModel, Field

from bystack.core.ports.transport import (
    ChannelEndpoint,
    TransportError,
    TransportHealth,
    TransportState,
)
from bystack.infra.transports.local_socket import UDS_BASE_URL


class SSHTunnelConfig(BaseModel):
    type: Literal["ssh_tunnel"] = "ssh_tunnel"
    host: str
    port: int = 22
    username: str | None = None
    client_key: str | None = Field(
        default=None, description="Path to a private key; falls back to the SSH agent"
    )
    known_hosts: str | None = Field(
        default=None,
        description="Path to known_hosts. Defaults to the user's known_hosts files.",
    )
    insecure_skip_host_key_check: bool = Field(
        default=False,
        description="Disables host key verification. Development only -- this accepts MITM.",
    )
    remote_socket: str = "/var/run/docker.sock"
    connect_timeout: float = 10.0


class SSHTunnelTransport:
    """Reaches a remote engine through an SSH-forwarded Unix socket."""

    __slots__ = ("_id", "_config", "_conn", "_listener", "_local_path", "_tmpdir", "_health")

    def __init__(self, transport_id: str, config: SSHTunnelConfig) -> None:
        self._id = transport_id
        self._config = config
        self._conn: asyncssh.SSHClientConnection | None = None
        self._listener: asyncssh.SSHListener | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._local_path: str | None = None
        self._health = TransportHealth(state=TransportState.CLOSED)

    @property
    def id(self) -> str:
        return self._id

    async def open(self) -> ChannelEndpoint:
        # Idempotent: reconnection loops call open() on every retry, and an
        # already-open tunnel must not spawn a second one.
        if self._conn is not None and self._local_path is not None:
            return ChannelEndpoint(base_url=UDS_BASE_URL, uds_path=self._local_path)

        cfg = self._config
        self._health = TransportHealth(state=TransportState.OPENING, since=time.time())

        # asyncssh's sentinels are a trap worth spelling out: `None` DISABLES
        # host key verification, while `()` selects the default known_hosts
        # files. Getting these backwards silently accepts any host key, so
        # the mapping is written out explicitly rather than inlined.
        if cfg.insecure_skip_host_key_check:
            known_hosts: object = None
        elif cfg.known_hosts:
            known_hosts = cfg.known_hosts
        else:
            known_hosts = ()

        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=cfg.host,
                    port=cfg.port,
                    username=cfg.username,
                    client_keys=[cfg.client_key] if cfg.client_key else None,
                    known_hosts=known_hosts,
                ),
                timeout=cfg.connect_timeout,
            )
        except TimeoutError as exc:
            raise self._fail(f"ssh connect to {cfg.host}:{cfg.port} timed out") from exc
        except (OSError, asyncssh.Error) as exc:
            raise self._fail(f"ssh connect to {cfg.host}:{cfg.port} failed: {exc}") from exc

        try:
            # The socket lives in a private 0700 directory: it is a
            # root-equivalent channel to the remote engine and must not be
            # openable by other local users.
            self._tmpdir = tempfile.TemporaryDirectory(prefix="bystack-")
            os.chmod(self._tmpdir.name, 0o700)
            self._local_path = str(Path(self._tmpdir.name) / "engine.sock")

            self._listener = await self._conn.forward_local_path(
                self._local_path, cfg.remote_socket
            )
        except (OSError, asyncssh.Error) as exc:
            await self.close()
            raise self._fail(
                f"failed forwarding {cfg.remote_socket} from {cfg.host}: {exc}"
            ) from exc

        self._health = TransportHealth(state=TransportState.OPEN, since=time.time())
        return ChannelEndpoint(base_url=UDS_BASE_URL, uds_path=self._local_path)

    async def close(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None

        if self._conn is not None:
            self._conn.close()
            with contextlib.suppress(Exception):
                await self._conn.wait_closed()
            self._conn = None

        if self._tmpdir is not None:
            with contextlib.suppress(Exception):
                self._tmpdir.cleanup()
            self._tmpdir = None

        self._local_path = None
        self._health = TransportHealth(state=TransportState.CLOSED, since=time.time())

    def health(self) -> TransportHealth:
        return self._health

    def _fail(self, detail: str) -> TransportError:
        self._health = TransportHealth(
            state=TransportState.FAILED, detail=detail, since=time.time()
        )
        return TransportError(detail)
