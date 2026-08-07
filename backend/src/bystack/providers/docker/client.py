"""Async Docker Engine API client.

ADR-0007 -- why not the official SDK: ``docker-py`` is synchronous and
requests-based. Calling it from an async-first collector either blocks the
event loop or forces every call through a thread pool, both of which
contradict the architecture and cost us threads we do not have budget for.
We consume a small, stable, well-documented subset of the Engine HTTP API
directly instead. The API is the official interface; only the client library
is declined.

The client is transport-agnostic: it is handed a
:class:`~bystack.core.ports.transport.ChannelEndpoint` and has no idea
whether the engine is local, behind SSH, or on an overlay network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

import httpx

from bystack.core.ports.transport import ChannelEndpoint

#: Engine API paths are used unversioned; the daemon maps them to the newest
#: version it supports. Pinning a version here would force a code change every
#: time a managed host upgrades Docker.
_JSON: Final = "application/json"

#: Deliberately small. Discovery is a handful of concurrent requests per host,
#: and at 3-10 hosts an oversized pool is pure resident memory.
_LIMITS: Final = httpx.Limits(max_connections=8, max_keepalive_connections=4)

#: Ordinary requests fail fast; a slow host must not stall the collector.
_REQUEST_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)

#: The event stream is long-lived and silent for hours at a time, so it must
#: have no read timeout -- a quiet cluster is not a broken one.
_STREAM_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)


class EngineError(RuntimeError):
    """The engine returned an error or could not be reached."""


class ActionResult(StrEnum):
    """How the engine answered a lifecycle request."""

    APPLIED = "applied"
    """``204``. The transition was performed."""

    UNCHANGED = "unchanged"
    """``304``. The container was already in the requested state.

    Docker is unusually precise here and it is worth preserving: starting a
    running container or stopping a stopped one is *not* an error and *not* a
    change. Folding this into success would report a restart that did nothing
    as a restart that worked.
    """

    NOT_FOUND = "not_found"
    """``404``. The container is gone -- almost always because it was
    recreated between the snapshot the operator clicked on and the request
    arriving. A race, not a fault."""

    CONFLICT = "conflict"
    """``409``. The engine refused the transition -- pausing a stopped
    container, unpausing a running one. Its message names the reason and is
    passed through verbatim rather than paraphrased."""

    ERROR = "error"
    """Anything else, including transport failure."""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """The engine's answer to one lifecycle request."""

    result: ActionResult
    detail: str | None = None


def _grace(timeout: float | None) -> dict[str, Any] | None:
    """Engine grace-period parameter, or nothing at all when unset."""
    return None if timeout is None else {"t": int(timeout)}


class EngineClient:
    """Thin async wrapper over the Docker Engine HTTP API."""

    __slots__ = ("_client",)

    def __init__(self, endpoint: ChannelEndpoint) -> None:
        transport = httpx.AsyncHTTPTransport(
            uds=endpoint.uds_path,
            limits=_LIMITS,
            verify=endpoint.ssl_context if endpoint.ssl_context is not None else True,
            retries=0,
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            timeout=_REQUEST_TIMEOUT,
            headers={"Accept": _JSON},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- discovery reads --------------------------------------------------

    async def info(self) -> dict[str, Any]:
        """``GET /info`` -- the source of the host's stable engine ID."""
        return await self._get_object("/info")

    async def version(self) -> dict[str, Any]:
        return await self._get_object("/version")

    async def list_containers(
        self, *, all_states: bool = True, ids: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """List containers, optionally narrowed to specific ids.

        Event handling reuses this rather than ``/containers/{id}/json``, so
        that list and watch produce the *same* payload shape and the mapper
        stays single-implementation. Two mappers for one entity is the kind of
        duplication that silently drifts apart over years.
        """
        params: dict[str, Any] = {"all": int(all_states)}
        if ids:
            params["filters"] = json.dumps({"id": list(ids)})
        return await self._get_array("/containers/json", params=params)

    async def inspect_container(self, container_id: str) -> dict[str, Any] | None:
        """Returns ``None`` if the container is already gone.

        A 404 here is the normal outcome of a race between an event arriving
        and the container being removed, not an error worth propagating.
        """
        return await self._get_optional(f"/containers/{container_id}/json")

    async def list_networks(self) -> list[dict[str, Any]]:
        return await self._get_array("/networks")

    async def inspect_network(self, network_id: str) -> dict[str, Any] | None:
        return await self._get_optional(f"/networks/{network_id}")

    async def list_volumes(self) -> list[dict[str, Any]]:
        # The one endpoint that wraps its array; `Volumes` is null, not [],
        # when the host has none.
        payload = await self._get_object("/volumes")
        return cast(list[dict[str, Any]], payload.get("Volumes") or [])

    async def list_images(self) -> list[dict[str, Any]]:
        return await self._get_array("/images/json")

    # -- container operations ---------------------------------------------
    #
    # The mutating half of the API. Every one of these returns an
    # `ActionOutcome` rather than raising, because the caller is executing
    # against one of many targets and a single refused transition must not
    # abort the rest of a stack-wide operation.

    async def start_container(self, container_id: str) -> ActionOutcome:
        return await self._action(f"/containers/{container_id}/start")

    async def stop_container(
        self, container_id: str, *, grace: float | None = None
    ) -> ActionOutcome:
        """Graceful stop: SIGTERM, then SIGKILL after the grace period.

        Named ``grace``, not ``timeout``, because it is neither: it is a
        value we hand to the daemon telling it how long to wait, and we go on
        waiting for the answer afterwards. Our own deadline is imposed a
        layer up, in the command service, and is deliberately larger than
        this.

        Omitted rather than defaulted when unset, so the engine applies its
        own 10s -- or whatever the operator configured for the container,
        which we have no business overriding.
        """
        return await self._action(
            f"/containers/{container_id}/stop", params=_grace(grace)
        )

    async def restart_container(
        self, container_id: str, *, grace: float | None = None
    ) -> ActionOutcome:
        return await self._action(
            f"/containers/{container_id}/restart", params=_grace(grace)
        )

    async def pause_container(self, container_id: str) -> ActionOutcome:
        return await self._action(f"/containers/{container_id}/pause")

    async def unpause_container(self, container_id: str) -> ActionOutcome:
        return await self._action(f"/containers/{container_id}/unpause")

    async def kill_container(
        self, container_id: str, *, signal: str | None = None
    ) -> ActionOutcome:
        params = {"signal": signal} if signal else None
        return await self._action(f"/containers/{container_id}/kill", params=params)

    # -- event stream -----------------------------------------------------

    async def events(
        self, *, since: float | None = None, types: tuple[str, ...] = ()
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``GET /events`` as decoded JSON objects.

        ``types`` is applied as a **server-side** filter so uninteresting
        traffic (exec_start, exec_die, health probes on chatty hosts) never
        crosses the socket or costs us a JSON parse. Filtering client-side
        would meet the same functional requirement at many times the CPU.
        """
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = f"{since:.9f}"
        if types:
            params["filters"] = json.dumps({"type": list(types)})

        try:
            async with self._client.stream(
                "GET", "/events", params=params, timeout=_STREAM_TIMEOUT
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # A malformed line is not worth tearing down a healthy
                        # stream over; the periodic reconcile will repair any
                        # state we miss because of it.
                        continue
        except httpx.HTTPError as exc:
            raise EngineError(f"event stream failed: {exc}") from exc

    # -- internals --------------------------------------------------------

    # `_get` returns whatever the daemon sent, which is `Any`. The Engine API's
    # response shapes are a contract we accept without a schema to check them
    # against, and these two wrappers are the single place that trust is
    # recorded -- so the reads above stay honestly typed, and `warn_return_any`
    # stays on for the rest of the provider code, where a leaked `Any` would
    # be a real defect rather than a documented boundary.
    async def _get_object(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], await self._get(path, params))

    async def _get_array(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._get(path, params))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise EngineError(f"GET {path} failed: {exc}") from exc

    async def _action(
        self, path: str, params: dict[str, Any] | None = None
    ) -> ActionOutcome:
        """POST a lifecycle transition and classify the answer.

        Does not use ``raise_for_status``. Every documented failure of these
        endpoints is a fact about the container worth reporting to the
        operator -- "already stopped", "no such container", "not paused" --
        and turning all of them into one exception would throw that away and
        force the caller to parse strings to get it back.
        """
        try:
            response = await self._client.post(path, params=params)
        except httpx.HTTPError as exc:
            return ActionOutcome(ActionResult.ERROR, f"{path} failed: {exc}")

        match response.status_code:
            case 204:
                return ActionOutcome(ActionResult.APPLIED)
            case 304:
                return ActionOutcome(ActionResult.UNCHANGED)
            case 404:
                return ActionOutcome(ActionResult.NOT_FOUND, _engine_message(response))
            case 409:
                return ActionOutcome(ActionResult.CONFLICT, _engine_message(response))
            case status:
                return ActionOutcome(
                    ActionResult.ERROR, f"engine returned {status}: {_engine_message(response)}"
                )

    async def _get_optional(self, path: str) -> Any | None:
        try:
            response = await self._client.get(path)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise EngineError(f"GET {path} failed: {exc}") from exc


def _engine_message(response: httpx.Response) -> str:
    """The daemon's own explanation, which is better than any we could write.

    Docker returns ``{"message": "..."}`` on error and the text is precise
    ("container abc is not paused"). Falls back to the raw body, because an
    unparseable error body during an incident is still evidence.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return response.text.strip() or f"HTTP {response.status_code}"
