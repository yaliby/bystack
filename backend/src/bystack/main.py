"""Entry point.

    python -m bystack                      # manages this machine
    python -m bystack --config bystack.yaml

Up to three listeners, sharing one process and one object graph, differing in
exactly one thing that matters -- what they require of whoever connects:

* the browser's, on loopback by default;
* the fleet's, with mutual TLS, when `agents.enabled` (MIGRATION section 3);
* a unix socket for the agent this Controller spawns for its own machine,
  where the file mode is the credential (MIGRATION section 4).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import ssl
import sys
from collections.abc import Iterator, Sequence
from typing import Final

import uvicorn

from bystack.api.app import (
    build_context,
    create_agent_app,
    create_app,
    create_local_agent_app,
)
from bystack.api.deps import AppContext
from bystack.api.tls import MutualTLSWebSocketProtocol, agent_ssl_context
from bystack.config import Settings

log = logging.getLogger(__name__)

#: How `--reload` hands the config path to the child it re-imports.
CONFIG_ENV: Final = "BYSTACK_CONFIG"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bystack", description="Infrastructure control plane")
    parser.add_argument("--config", help="Path to bystack.yaml; omit to start with no hosts")
    parser.add_argument("--host", help="Override the configured bind address")
    parser.add_argument("--port", type=int, help="Override the configured port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload (development)")
    return parser.parse_args(argv)


class _Managed(uvicorn.Server):
    """A server that does not touch signal handlers.

    Two `uvicorn.Server` instances in one loop both install a SIGINT handler,
    and the second one wins -- so Ctrl-C would stop one listener and leave the
    other running. Signals belong to :func:`serve`, which shuts all of them
    down together; see :func:`_stop_on_signal` for why not even the primary
    may keep uvicorn's own handling.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _stop_on_signal(servers: Sequence[uvicorn.Server]) -> None:
    """Make SIGINT and SIGTERM an ordinary return from `Server.serve()`.

    uvicorn's own handling cannot do this job. `serve()` restores the handler
    it replaced and then re-raises the signal it caught, so the *default*
    disposition terminates the process at that moment -- after the listener
    has closed but before anything the caller arranged to run afterwards. The
    visible symptom was a SIGTERM leaving the local agent's socket in the
    state directory, and it is a systemd `stop` rather than an exotic case.

    Owning the signal here makes the return from `serve()` an ordinary return,
    so the cleanup in :func:`serve` runs. A second signal is the escape hatch
    for a shutdown that is itself stuck, and keeps Ctrl-C twice meaning what
    everyone expects it to mean.
    """
    forcing = False

    def request_stop(*_: object) -> None:
        nonlocal forcing
        for server in servers:
            server.should_exit = True
            if forcing:
                server.force_exit = True
        if not forcing:
            forcing = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)


def agent_listener(context: AppContext, log_level: str) -> _Managed:
    """The mutually-authenticated listener, with its certificate in hand.

    The server certificate is issued by the same internal CA that signs agent
    certificates, and reissued here if it is stale or the configured names
    have changed -- so the operator's only job is listing the names agents
    will dial, and `agents.server_names` says so.
    """
    settings = context.settings
    credentials = context.trust.ca.server_credentials(settings.agents.server_names)
    host, port = settings.agents.listen_address

    config = uvicorn.Config(
        create_agent_app(context),
        host=host,
        port=port,
        log_level=log_level,
        # uvicorn decides it is serving TLS from these two, and then builds a
        # context we replace below. Passing them is not redundant: `is_ssl`
        # gates the whole TLS path, including how the bind is reported.
        ssl_certfile=str(credentials.certificate),
        ssl_keyfile=str(credentials.key),
        ssl_ca_certs=str(context.trust.ca.ca_certificate_path),
        ssl_cert_reqs=ssl.CERT_OPTIONAL,
        ws=MutualTLSWebSocketProtocol,
    )
    config.load()
    # Every TLS decision in one reviewable function rather than spread across
    # six keyword arguments -- which is also what lets `agent_ssl_context`
    # require TLS 1.3 and explain why.
    config.ssl = agent_ssl_context(credentials, context.trust.ca.ca_certificate_path)
    return _Managed(config)


def local_listener(context: AppContext, log_level: str) -> tuple[_Managed, socket.socket] | None:
    """The unix socket the Controller's own agent dials, if there is one.

    The socket is bound by :class:`LocalAgent` rather than by uvicorn, because
    its mode is the whole of this connection's authentication and uvicorn opens
    a unix socket world-writable. Passing an already-bound socket is also what
    removes the startup race: the child cannot dial before the listener exists,
    because the listener exists before the child does.
    """
    listener = context.local_agent.bind()
    if listener is None:
        return None

    config = uvicorn.Config(
        create_local_agent_app(context),
        log_level=log_level,
        # No proxy, no forwarded headers, no access log. The only client is a
        # process this Controller started.
        access_log=False,
    )
    config.load()
    return _Managed(config), listener


async def serve(settings: Settings, host: str | None, port: int | None) -> None:
    context = build_context(settings)
    primary = _Managed(
        uvicorn.Config(
            create_app(settings, context),
            host=host or settings.api.host,
            port=port or settings.api.port,
            log_level=settings.log_level.lower(),
        )
    )

    background: list[asyncio.Task[None]] = []
    servers: list[_Managed] = []
    log_level = settings.log_level.lower()

    local = local_listener(context, log_level)
    if local is not None:
        server, listener = local
        servers.append(server)
        background.append(
            asyncio.create_task(server.serve(sockets=[listener]), name="local-agent-listener")
        )
        # Started after its listener and before anything else, so the first
        # thing a zero-config run does is fill the graph with this machine.
        await context.local_agent.start()
    else:
        log.info("local agent: %s", context.local_agent.status.detail)

    if settings.agents.enabled:
        server = agent_listener(context, log_level)
        servers.append(server)
        background.append(asyncio.create_task(server.serve(), name="agent-listener"))
        listen_host, listen_port = settings.agents.listen_address
        log.info(
            "agents dial wss://%s:%d (mutual TLS, CA fingerprint %s)",
            listen_host, listen_port, context.trust.ca.fingerprint[:16],
        )
    else:
        log.info(
            "the fleet listener is off (agents.enabled); only this machine is managed. "
            "Mint a token with POST %s and set agents.enabled: true to add hosts",
            "/api/v1/agents/tokens",
        )

    # After the secondaries exist, so one Ctrl-C stops all three.
    _stop_on_signal([primary, *servers])

    try:
        await primary.serve()
    finally:
        # The primary owns the signals, so its return is the shutdown signal
        # for everything else. Without this the process would hang on a
        # listener nobody asked to stop.
        #
        # The child first: it must not spend a reconnect backoff dialling a
        # socket that is being torn down underneath it.
        await context.local_agent.stop()
        for server in servers:
            server.should_exit = True
        for task in background:
            await task


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        settings = Settings.load(args.config) if args.config else Settings.default()
    except (FileNotFoundError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.reload:
        # Reload re-imports the app in a child process, so it needs an import
        # string rather than an object -- which is why the flag did nothing
        # before. The child gets the config path through the environment
        # because an import string cannot carry an argument.
        #
        # The browser-facing app only. A reloading child that also rebound a
        # TLS listener and reopened the CA on every keystroke is not a
        # development convenience.
        if args.config:
            os.environ[CONFIG_ENV] = args.config
        uvicorn.run(
            "bystack.main:reload_target",
            factory=True,
            host=args.host or settings.api.host,
            port=args.port or settings.api.port,
            log_level=settings.log_level.lower(),
            reload=True,
        )
        return 0

    asyncio.run(serve(settings, args.host, args.port))
    return 0


def reload_target() -> object:
    """The browser-facing app alone, for `--reload`. No agent listener."""
    path = os.environ.get(CONFIG_ENV)
    return create_app(Settings.load(path) if path else Settings.default())


if __name__ == "__main__":
    raise SystemExit(main())
