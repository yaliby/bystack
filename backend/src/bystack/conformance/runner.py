"""Conformance run: does this agent binary behave?

    python -m bystack.conformance ../agent/target/release/bystack-agent

Starts a scripted Docker Engine and a Controller, launches the agent between
them, and drives it through the scenarios that matter. Prints a checklist.

This exists so that writing an agent -- in any language -- is a matter of
running one command and fixing what it says. Three of the checks below are for
failures that are **invisible at runtime**: the agent works perfectly, the
topology is correct, and it costs a hundred times more than it should or
silently loses a change. No amount of manual testing surfaces those, which is
the whole argument for having this.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from bystack.api.app import API_PREFIX, create_app
from bystack.api.deps import AppContext
from bystack.config import AgentsConfig, Settings
from bystack.conformance.engine import (
    DEFAULT_IMAGE,
    DEFAULT_NETWORK,
    DEFAULT_VOLUME,
    ScriptedEngine,
    make_container,
)
from bystack.core.graph.model import Node
from bystack.core.graph.store import GraphStore
from bystack.core.identity import NodeKind
from bystack.runtime.collector import Collector

C1 = "c" * 64
C2 = "d" * 64

#: How long to wait for the graph to reflect something. Generous: a failing
#: check should mean "the agent does not do this", never "the machine was
#: busy".
SETTLE = 6.0


@dataclass
class Check:
    name: str
    why: str
    passed: bool
    detail: str = ""


class Harness:
    def __init__(self, engine: ScriptedEngine, app: FastAPI, port: int) -> None:
        self.engine = engine
        self.app = app
        self.port = port
        self.checks: list[Check] = []

    @property
    def store(self) -> GraphStore:
        context: AppContext = self.app.state.context
        return context.store

    @property
    def collector(self) -> Collector:
        context: AppContext = self.app.state.context
        return context.collector

    def nodes_of(self, kind: str) -> list[Node]:
        return [n for n in self.store.snapshot().nodes if n.kind == kind]

    async def until(self, predicate: Callable[[], bool], within: float = SETTLE) -> bool:
        """Poll until true.

        Agent frames are fire-and-forget in both directions, so there is
        nothing to await except the effect. `within` is a budget for the
        agent to react, not a timeout on an operation -- which is why it is
        not `asyncio.timeout`.
        """
        deadline = asyncio.get_running_loop().time() + within
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return predicate()

    def record(self, name: str, why: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, why, passed, detail))


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


async def scenario_initial_sync(h: Harness) -> None:
    reached = await h.until(lambda: len(h.nodes_of(NodeKind.CONTAINER)) == 1)
    h.record(
        "initial sync",
        "the first frame of every connection must be a full Sync",
        reached,
        f"{len(h.nodes_of(NodeKind.CONTAINER))} container(s) in the graph",
    )

    h.record(
        "logical layer",
        "compose labels must survive the wire so the Controller can derive services",
        bool(h.nodes_of(NodeKind.SERVICE)) and bool(h.nodes_of(NodeKind.STACK)),
    )

    # Waited for separately. Each slice is its own frame, so the container
    # Sync landing says nothing about whether the volume Sync has been applied
    # yet -- and asserting on it in the same tick tests our scheduling, not
    # the agent.
    every_slice = await h.until(
        lambda: bool(h.nodes_of(NodeKind.NETWORK)) and bool(h.nodes_of(NodeKind.VOLUME))
    )
    h.record(
        "every slice",
        "networks, volumes and images are separate Sync frames, not an afterthought",
        every_slice,
        f"{len(h.nodes_of(NodeKind.NETWORK))} network(s), "
        f"{len(h.nodes_of(NodeKind.VOLUME))} volume(s)",
    )


async def scenario_watch(h: Harness) -> None:
    await h.engine.watching.wait()
    h.record(
        "watch established",
        "without an event stream the agent is a polling loop with extra steps",
        True,
    )

    before = h.store.seq
    h.engine.containers.append(make_container(C2, "shop-db-1", service="db"))
    await h.engine.emit("container")

    reached = await h.until(lambda: len(h.nodes_of(NodeKind.CONTAINER)) == 2)
    h.record(
        "event -> delta",
        "an event must trigger a re-List of its slice and a frame",
        reached,
        f"seq {before} -> {h.store.seq}",
    )


async def scenario_status_string_is_not_hashed(h: Harness) -> None:
    """The expensive one.

    Docker's `Status` is a rendered string that changes on a wall clock. An
    agent that hashes it re-sends every container on every resync, per host,
    forever -- and nothing anywhere reports an error. This is the check that
    catches it.
    """
    for container in h.engine.containers:
        if container["State"] == "running":
            container["Status"] = "Up 9 hours"

    before = h.store.seq
    await h.engine.emit("container")
    await asyncio.sleep(1.5)

    h.record(
        "status string excluded from the hash",
        "hashing Docker's rendered status re-sends everything on a timer, silently",
        h.store.seq == before,
        f"seq {before} -> {h.store.seq} (must not move)",
    )


async def scenario_steady_state_is_silent(h: Harness) -> None:
    before = h.store.seq
    await h.engine.emit("container")
    await asyncio.sleep(1.5)

    h.record(
        "quiet host, quiet wire",
        "a re-List that finds nothing must produce no graph change",
        h.store.seq == before,
        f"seq {before} -> {h.store.seq}",
    )


async def scenario_coalescing(h: Harness) -> None:
    """A `compose up` is a hundred events in a fraction of a second."""
    before = h.engine.recorded.count("/containers/json")
    for _ in range(40):
        await h.engine.emit("container")
    await asyncio.sleep(1.5)

    lists = h.engine.recorded.count("/containers/json") - before
    h.record(
        "burst coalescing",
        "40 events must cost one or two Lists, not forty",
        lists <= 3,
        f"{lists} List(s) for 40 events",
    )


async def scenario_removal(h: Harness) -> None:
    h.engine.containers = [c for c in h.engine.containers if c["Id"] != C2]
    await h.engine.emit("container")

    reached = await h.until(lambda: len(h.nodes_of(NodeKind.CONTAINER)) == 1)
    h.record(
        "membership carries deletion",
        "an id that stops appearing in the delta is gone; no separate signal exists",
        reached,
        f"{len(h.nodes_of(NodeKind.CONTAINER))} container(s) remain",
    )

    h.record(
        "orphaned service removed",
        "the last container of a service disappearing must take the service with it",
        len(h.nodes_of(NodeKind.SERVICE)) == 1,
    )


async def scenario_command(h: Harness) -> None:
    from bystack.core.ports.command import CommandKind, CommandRequest

    service = h.app.state.context.commands
    target = next(iter(h.nodes_of(NodeKind.CONTAINER)))

    result = await service.execute(
        CommandRequest(kind=CommandKind.STOP, target=target.urn, reason="conformance")
    )
    h.record(
        "command round trip",
        "a command must reach the socket and its result must find its way back",
        result.ok,
        f"{result.status}: {result.outcomes[0].detail or 'ok'}",
    )

    stopped = any(
        c["Id"] == target.urn.segments[-1] and c["State"] == "exited"
        for c in h.engine.containers
    )
    h.record(
        "command reached the engine",
        "the agent must act on the daemon, not merely acknowledge",
        stopped,
    )

    repeat = await service.execute(
        CommandRequest(kind=CommandKind.STOP, target=target.urn)
    )
    h.record(
        "304 preserved",
        "stopping an already-stopped container is a noop, not a success",
        repeat.outcomes[0].status.value == "noop",
        f"reported {repeat.outcomes[0].status}",
    )


async def scenario_reconnect(h: Harness) -> None:
    """Kill the event stream and confirm the agent notices.

    A dead watch is indistinguishable from a quiet host unless the agent is
    actively checking, which is exactly why this is easy to get wrong.
    """
    h.engine.watching.clear()
    await h.engine.drop_event_stream()

    reattached = await h.until(lambda: h.engine.watching.is_set(), within=15.0)
    h.record(
        "recovers from a dropped watch",
        "a dead event stream must be noticed, not mistaken for an idle host",
        reattached,
    )


SCENARIOS: list[tuple[str, Callable[[Harness], Awaitable[None]]]] = [
    ("initial sync", scenario_initial_sync),
    ("watch", scenario_watch),
    ("status hash", scenario_status_string_is_not_hashed),
    ("steady state", scenario_steady_state_is_silent),
    ("coalescing", scenario_coalescing),
    ("removal", scenario_removal),
    ("commands", scenario_command),
    ("reconnect", scenario_reconnect),
]


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


async def run(agent_binary: Path, port: int, keep_going: bool) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="bystack-conformance-"))
    socket_path = workdir / "docker.sock"

    engine = ScriptedEngine(
        socket_path,
        containers=[make_container(C1, "shop-web-1")],
        networks=[DEFAULT_NETWORK],
        volumes=[DEFAULT_VOLUME],
        images=[DEFAULT_IMAGE],
    )
    await engine.start()

    app = create_app(
        Settings(
            hosts=[],
            read_only=False,
            agents=AgentsConfig(enabled=True, auto_approve=True, resync_interval=60),
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    # uvicorn signals readiness with a plain attribute rather than an event,
    # so there is nothing here to await on.
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.05)

    url = f"ws://127.0.0.1:{port}{API_PREFIX}/agents/connect"
    print(f"engine socket : {socket_path}")
    print(f"controller    : {url}")
    print(f"agent         : {agent_binary}\n")

    agent = await asyncio.create_subprocess_exec(
        str(agent_binary),
        "--controller", url,
        "--socket", str(socket_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "RUST_BACKTRACE": "1"},
    )

    harness = Harness(engine, app, port)
    try:
        for name, scenario in SCENARIOS:
            if agent.returncode is not None:
                harness.record(name, "agent exited early", False, f"exit {agent.returncode}")
                if not keep_going:
                    break
                continue
            await scenario(harness)
            if not keep_going and any(not c.passed for c in harness.checks):
                break
    finally:
        with contextlib.suppress(ProcessLookupError):
            agent.terminate()
        # The pipe is requested above, so `stdout` is never None in practice --
        # but this block runs on the failure path, where the agent's output is
        # the only diagnostic there is. It must not be what raises.
        if agent.stdout is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                output = await asyncio.wait_for(agent.stdout.read(), timeout=2.0)
                if output:
                    print("--- agent output ---")
                    print(output.decode().rstrip())
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serving, timeout=5.0)
        await engine.stop()

    return report(harness)


def report(harness: Harness) -> int:
    print("\n--- conformance ---")
    failed = 0
    for check in harness.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}")
        if check.detail:
            print(f"         {check.detail}")
        if not check.passed:
            failed += 1
            print(f"         why it matters: {check.why}")

    total = len(harness.checks)
    print(f"\n{total - failed}/{total} checks passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", type=Path, help="path to the agent binary")
    parser.add_argument("--port", type=int, default=8130)
    parser.add_argument(
        "--keep-going", action="store_true", help="run every scenario even after a failure"
    )
    args = parser.parse_args(argv)

    if not args.agent.exists():
        print(f"no such agent binary: {args.agent}", file=sys.stderr)
        return 2

    return asyncio.run(run(args.agent, args.port, args.keep_going))


if __name__ == "__main__":
    raise SystemExit(main())
