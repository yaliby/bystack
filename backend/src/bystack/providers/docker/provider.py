"""Docker provider.

Owns one engine's graph partition. Knows nothing about any other provider,
about visualization, or about how it is reached on the network.
"""

from __future__ import annotations

import asyncio
import contextlib

from bystack.core.graph.model import Node
from bystack.core.ports.command import (
    CommandKind,
    CommandRejected,
    CommandRequest,
    RejectionReason,
    TargetOutcome,
)
from bystack.core.ports.provider import GraphWriter, ProviderHealth, ProviderState
from bystack.core.ports.transport import Transport
from bystack.providers.docker import commands
from bystack.providers.docker.informer import DockerInformer

KIND = "docker"


class DockerProvider:
    """A :class:`~bystack.core.ports.provider.Provider` over one engine."""

    __slots__ = ("_id", "_informer", "_task")

    def __init__(
        self,
        provider_id: str,
        transport: Transport,
        writer: GraphWriter,
        *,
        resync_interval: float = 300.0,
        **informer_kwargs: object,
    ) -> None:
        self._id = provider_id
        self._informer = DockerInformer(
            provider_id,
            transport,
            writer,
            resync_interval=resync_interval,
            **informer_kwargs,  # type: ignore[arg-type]
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def kind(self) -> str:
        return KIND

    async def start(self) -> None:
        """Start discovery in the background and return immediately.

        Returning promptly is a hard requirement, not a nicety: one
        unreachable host must never delay startup of the other nine, and the
        API must come up and report DEGRADED rather than hang.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._informer.run(), name=f"informer:{self._id}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def health(self) -> ProviderHealth:
        if self._task is None:
            return ProviderHealth(state=ProviderState.STOPPED)
        return self._informer.health()

    # -- CommandExecutor ---------------------------------------------------
    #
    # Implementing this protocol is what makes the provider writable. A
    # provider that omits both methods is read-only by construction, which is
    # the correct and effortless default for observational sources.

    def supported_commands(self, node: Node) -> frozenset[CommandKind]:
        """What can be done to this node, given its state and our connection.

        A disconnected provider supports nothing. Reporting otherwise would
        have the UI offer buttons that are certain to be rejected -- and
        during an outage, "restart it" is precisely what an operator will
        try, so the answer needs to be honest before the click, not after.
        """
        if self._informer.client is None:
            return frozenset()
        return commands.supported_commands(node)

    async def execute(self, request: CommandRequest, target: Node) -> TargetOutcome:
        client = self._informer.client
        if client is None:
            raise CommandRejected(
                RejectionReason.PROVIDER_UNAVAILABLE,
                f"host {self._id} is not currently connected",
            )
        return await commands.execute(client, request, target)
