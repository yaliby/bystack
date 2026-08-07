"""Local Unix socket transport.

The degenerate case: the engine is already reachable locally, so opening the
transport is a validation step rather than a connection step. It exists as a
first-class transport anyway, because the moment providers are allowed to
special-case "local" the abstraction is dead.
"""

from __future__ import annotations

import os
import stat
import time
from typing import Literal

from pydantic import BaseModel, Field

from bystack.core.ports.transport import (
    ChannelEndpoint,
    TransportError,
    TransportHealth,
    TransportState,
)

#: Placeholder authority for HTTP-over-UDS. The host part is never resolved;
#: httpx requires a syntactically valid URL and routes over the socket.
UDS_BASE_URL = "http://docker"


class LocalSocketConfig(BaseModel):
    type: Literal["local_socket"] = "local_socket"
    path: str = Field(default="/var/run/docker.sock", description="Path to the engine socket")


class LocalSocketTransport:
    """Reaches an engine over a Unix domain socket on this machine."""

    __slots__ = ("_id", "_path", "_health")

    def __init__(self, transport_id: str, config: LocalSocketConfig) -> None:
        self._id = transport_id
        self._path = config.path
        self._health = TransportHealth(state=TransportState.CLOSED)

    @property
    def id(self) -> str:
        return self._id

    async def open(self) -> ChannelEndpoint:
        # These are three stat calls against a local path -- microseconds, and
        # never a remote or networked filesystem. Dispatching them to a worker
        # thread to satisfy the "no blocking calls in async" rule would cost
        # more than the calls themselves.
        self._validate()  # noqa: ASYNC240
        self._health = TransportHealth(state=TransportState.OPEN, since=time.time())
        return ChannelEndpoint(base_url=UDS_BASE_URL, uds_path=self._path)

    async def close(self) -> None:
        self._health = TransportHealth(state=TransportState.CLOSED, since=time.time())

    def _validate(self) -> None:
        """Fail with a precise, actionable message rather than a vague one."""
        if not os.path.exists(self._path):
            raise self._fail(f"socket {self._path} does not exist; is Docker running?")

        if not stat.S_ISSOCK(os.stat(self._path).st_mode):
            raise self._fail(f"{self._path} exists but is not a socket")

        # Checked eagerly because the failure mode is extremely common -- the
        # user is simply not in the `docker` group -- and the alternative is
        # an opaque connection error at the first request.
        if not os.access(self._path, os.R_OK | os.W_OK):
            raise self._fail(
                f"no permission to access {self._path}. "
                f"Add your user to the 'docker' group and re-login: "
                f"sudo usermod -aG docker $USER"
            )

    def health(self) -> TransportHealth:
        return self._health

    def _fail(self, detail: str) -> TransportError:
        self._health = TransportHealth(
            state=TransportState.FAILED, detail=detail, since=time.time()
        )
        return TransportError(detail)
