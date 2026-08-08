"""A real Controller for the conformance runs, with real mutual TLS.

Shared by `bystack.conformance` and `bystack.conformance.fleet`, which need
the same thing: a listener an agent binary can actually dial.

Since ADR-0011 that means the whole trust path -- a CA, a server certificate,
a join token, an enrollment -- and it would have been tempting to add a
loopback exemption instead. There is no such exemption in the Controller and
there should not be one here, because then the conformance suite would be
verifying an agent that never authenticates against a Controller that never
asks. The setup below is a dozen lines, and it means "the agent enrolls
correctly" is checked on every run rather than never.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from bystack.api.app import build_context, create_app
from bystack.api.deps import AppContext
from bystack.config import AgentsConfig, Settings
from bystack.main import agent_listener

#: Long enough that a slow machine still enrolls, short enough to stay honest
#: about what a join token is.
TOKEN_TTL = dt.timedelta(minutes=10)


@dataclass
class Controller:
    """One Controller, one listener, and a way to hand an agent its arguments."""

    context: AppContext
    app: FastAPI
    url: str
    state_dir: Path
    _server: object
    _serving: asyncio.Task[None]

    def agent_args(self, binary: Path, socket: Path, name: str) -> list[str]:
        """Everything the binary needs to enrol and connect, as one host.

        Each host gets its own state directory. Sharing one would have the
        agents overwrite each other's certificates, which is not a scenario --
        it is a harness bug that would look like an authentication bug.
        """
        certificates = self.state_dir / name
        certificates.mkdir(parents=True, exist_ok=True)
        token = self.context.trust.mint_token(TOKEN_TTL)
        return [
            str(binary),
            "--controller", self.url,
            "--token", token.token,
            "--state-dir", str(certificates),
            "--socket", str(socket),
        ]

    async def stop(self) -> None:
        self._server.should_exit = True  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._serving, timeout=5.0)
        await self.context.collector.stop()
        await self.context.bus.aclose()


async def start(workdir: Path, port: int, *, resync_interval: int = 60) -> Controller:
    state_dir = workdir / "controller"
    settings = Settings(
        read_only=False,
        agents=AgentsConfig(
            enabled=True,
            # The agent's *approval* is not what these runs are about, and
            # leaving every host pending would make every scenario fail on the
            # same uninteresting thing. Enrollment itself is still exercised in
            # full: token, CSR, certificate, mutual handshake.
            auto_approve=True,
            resync_interval=resync_interval,
            state_dir=str(state_dir),
            listen=f"127.0.0.1:{port}",
            server_names=["127.0.0.1", "localhost"],
        ),
    )
    context = build_context(settings)

    # Built but not served. The scenarios read the store and the collector off
    # it directly, and a second listener nothing dials would only be a port to
    # collide with.
    app = create_app(settings, context)
    await context.collector.start()

    server = agent_listener(context, "warning")
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn signals with an attribute
        await asyncio.sleep(0.05)

    return Controller(
        context=context,
        app=app,
        url=f"wss://127.0.0.1:{port}",
        state_dir=state_dir,
        _server=server,
        _serving=serving,
    )
