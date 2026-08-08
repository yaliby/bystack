"""Collector runtime.

Supervises every provider. This is the component that becomes its own process
when the deployment grows past a single instance; because everything above it
talks only to :class:`~bystack.core.graph.store.GraphStore` and the event bus,
that split is a change to the composition root and nothing else.
"""

from __future__ import annotations

import asyncio
import logging

from bystack.config import Settings
from bystack.core.graph.store import GraphStore
from bystack.core.identity import engine_scope
from bystack.core.ports.eventbus import EventBus
from bystack.core.ports.provider import Provider, ProviderHealth
from bystack.providers.agent.provider import AgentProvider
from bystack.runtime.writer import PartitionWriter

log = logging.getLogger(__name__)


class Collector:
    """Owns provider lifecycle."""

    __slots__ = ("_store", "_bus", "_providers")

    def __init__(self, store: GraphStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus
        self._providers: dict[str, Provider] = {}

    @classmethod
    def from_settings(cls, settings: Settings, store: GraphStore, bus: EventBus) -> Collector:
        """A Collector with no providers at all.

        There is nothing left to build from configuration: hosts are not
        configured any more, they arrive. Every provider in this registry is
        created by an agent connecting (`agent_provider`), which is why this
        no longer reads `settings` for anything.
        """
        del settings
        return cls(store, bus)

    def agent_provider(self, engine_id: str, *, create: bool) -> AgentProvider | None:
        """The provider for an enrolled host, optionally adopting a new one.

        This is where the Collector stops being a supervisor of outbound
        informers and becomes an **agent registry**: it holds one provider per
        enrolled host and binds inbound connections to them
        (`docs/MIGRATION.md` §2).

        ``create=False`` is the refusal path for an agent we do not know. It
        is no longer the security control it was before ADR-0011 landed --
        enrollment and admission are, in `runtime/trust.py`, and by the time
        anything calls this with ``create=True`` the engine id has come out of
        a certificate this Controller signed. It stays an explicit parameter
        anyway: the caller is asserting that it checked, and a default would
        make that assertion by omission.

        The partition key is the engine id, which is also the agent's identity
        under ADR-0011. An agent reinstalled on the same host therefore keeps
        its partition; a host rebuilt from scratch is correctly a new one.

        The id is normalized here rather than by the caller. Docker shipped two
        formats for it -- a colon-delimited fingerprint before 25.0, a UUID
        since -- and `engine_scope` collapses them, because a colon is a URN
        separator. Skipping it would give a pre-25.0 host two spellings of its
        own identity: one in every URN, another in `node.source`, with
        `/graph?sources=` and the UI's per-host grouping quietly matching
        neither.
        """
        engine_id = engine_scope(engine_id)
        existing = self._providers.get(engine_id)
        if existing is not None:
            if isinstance(existing, AgentProvider):
                return existing
            # An engine id colliding with a configured Docker host. During the
            # migration both paths run, and letting an agent take over a
            # partition the SSH path is actively writing would have the two
            # implementations fight over it -- which is exactly the comparison
            # MIGRATION §6 wants to run cleanly.
            log.warning(
                "agent %s collides with a configured %s provider; refusing",
                engine_id, existing.kind,
            )
            return None

        if not create:
            return None

        provider = AgentProvider(engine_id, self.writer_for(engine_id))
        self.register(provider)
        log.info("adopted agent-backed host %s", engine_id)
        return provider

    def register(self, provider: Provider) -> Provider:
        """Adopt a provider built elsewhere.

        The provider set is not config-derived under the agent model: agents
        dial in at runtime and their providers are created when they enroll,
        not when the file is read.
        """
        if provider.id in self._providers:
            raise ValueError(f"duplicate provider id {provider.id!r}")
        self._providers[provider.id] = provider
        return provider

    def writer_for(self, source: str) -> PartitionWriter:
        """A writer bound to one partition, and structurally to no other."""
        return PartitionWriter(self._store, self._bus, source)

    async def start(self) -> None:
        """Start every provider concurrently.

        Failures are absorbed here rather than propagated: a control plane
        that refuses to start because one of ten hosts is powered off is
        useless precisely when it is most needed.
        """
        results = await asyncio.gather(
            *(p.start() for p in self._providers.values()), return_exceptions=True
        )
        for provider, result in zip(self._providers.values(), results, strict=True):
            if isinstance(result, BaseException):
                log.error("provider %s failed to start: %r", provider.id, result)

    async def stop(self) -> None:
        await asyncio.gather(
            *(p.stop() for p in self._providers.values()), return_exceptions=True
        )

    @property
    def providers(self) -> dict[str, Provider]:
        return dict(self._providers)

    def health(self) -> dict[str, ProviderHealth]:
        return {pid: p.health() for pid, p in self._providers.items()}
