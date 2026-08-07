"""Test fixtures.

Nothing here touches Docker, the network, or the filesystem. If a test in
this suite ever needs a daemon, an abstraction has leaked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from bystack.core.ports.transport import ChannelEndpoint, TransportHealth, TransportState

#: Exercises the pre-25.0 colon-delimited engine ID format end to end, so
#: the normalization is covered by every mapper and informer test, not just
#: the one that names it.
ENGINE_ID = "AAAA:BBBB:CCCC"
ENGINE_ID_SAFE = "AAAABBBBCCCC"


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


class FakeTransport:
    """A transport that opens instantly and connects to nothing."""

    def __init__(self, transport_id: str = "fake") -> None:
        self._id = transport_id
        self.open_count = 0
        self.close_count = 0

    @property
    def id(self) -> str:
        return self._id

    async def open(self) -> ChannelEndpoint:
        self.open_count += 1
        return ChannelEndpoint(base_url="http://docker", uds_path="/nonexistent.sock")

    async def close(self) -> None:
        self.close_count += 1

    def health(self) -> TransportHealth:
        return TransportHealth(state=TransportState.OPEN)


class FakeEngineClient:
    """A scripted Docker Engine.

    Records every call so tests can assert on *how much* the informer talks to
    the daemon, not only on the resulting graph -- the resource budget is a
    requirement, so call counts are worth asserting on.
    """

    def __init__(
        self,
        info: dict[str, Any],
        containers: Sequence[dict[str, Any]] = (),
        networks: Sequence[dict[str, Any]] = (),
        volumes: Sequence[dict[str, Any]] = (),
        images: Sequence[dict[str, Any]] = (),
        events: Sequence[dict[str, Any]] = (),
    ) -> None:
        self._info = info
        self.containers = list(containers)
        self.networks = list(networks)
        self.volumes = list(volumes)
        self.images = list(images)
        self._events = list(events)
        self.calls: dict[str, int] = {}
        self.closed = False
        #: Set once the event stream has been drained, so tests can wait for
        #: the informer to have consumed everything rather than sleeping.
        self.events_drained = asyncio.Event()

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def info(self) -> dict[str, Any]:
        self._count("info")
        return self._info

    async def list_containers(
        self, *, all_states: bool = True, ids: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        self._count("list_containers")
        if ids:
            return [c for c in self.containers if c["Id"] in ids]
        return list(self.containers)

    async def list_networks(self) -> list[dict[str, Any]]:
        self._count("list_networks")
        return list(self.networks)

    async def list_volumes(self) -> list[dict[str, Any]]:
        self._count("list_volumes")
        return list(self.volumes)

    async def list_images(self) -> list[dict[str, Any]]:
        self._count("list_images")
        return list(self.images)

    async def events(
        self, *, since: float | None = None, types: tuple[str, ...] = ()
    ) -> AsyncIterator[dict[str, Any]]:
        self._count("events")
        for event in self._events:
            yield event
        self.events_drained.set()
        # Then block forever, like a real quiet daemon. Returning here would
        # make the informer treat the stream as closed and reconnect.
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True
