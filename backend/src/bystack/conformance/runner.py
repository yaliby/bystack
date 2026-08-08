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
from pathlib import Path

from fastapi import FastAPI

from bystack.api.deps import AppContext
from bystack.conformance import controller as conformance_controller
from bystack.conformance.engine import (
    DEFAULT_IMAGE,
    DEFAULT_NETWORK,
    DEFAULT_VOLUME,
    ScriptedEngine,
    make_container,
)
from bystack.conformance.report import Check, report
from bystack.core.graph.model import Node
from bystack.core.graph.store import GraphStore
from bystack.core.identity import NodeKind
from bystack.runtime.collector import Collector

C1 = "c" * 64
C2 = "d" * 64
#: The crash-loop pair: one container restarting, one beside it that is not.
#: Both are needed — "inspects only what is looping" is not a claim a
#: single-container fixture can make.
LOOPING = "e" * 64
SETTLED = "f" * 64

#: How long to wait for the graph to reflect something. Generous: a failing
#: check should mean "the agent does not do this", never "the machine was
#: busy".
SETTLE = 6.0


class Harness:
    def __init__(
        self,
        engine: ScriptedEngine,
        app: FastAPI,
        port: int,
        *,
        binary: Path,
        pid: int | None = None,
    ) -> None:
        self.engine = engine
        self.app = app
        self.port = port
        # The binary and the process running it, for the budget checks. Held
        # here rather than passed to one scenario, because a scenario
        # signature that varies is a scenario list that cannot be reordered.
        self.binary = binary
        self.pid = pid
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


async def scenario_health_is_hashed(h: Harness) -> None:
    """The mirror image of the check above, and the opposite conclusion.

    Both are about a field beside `state` that `state` does not cover, and
    they are pulled apart by one question: does it change on a clock, or on
    an event? The status line changes on a clock and hashing it costs 100x
    for nothing. The healthcheck's verdict changes when the verdict changes,
    and *not* hashing it costs correctness -- a container stays `running`
    while it fails its probe, so an agent with the verdict outside the hash
    has nothing new to send and the map goes on drawing it green.

    Neither failure raises anything. This is the check that catches the
    second one, and it belongs beside the first so that whoever is tempted
    to "make the hashing consistent" finds both reasons in one place.
    """
    for container in h.engine.containers:
        if container["State"] == "running":
            container["Health"] = {"Status": "unhealthy", "FailingStreak": 3}

    before = h.store.seq
    await h.engine.emit("container")
    moved = await h.until(lambda: h.store.seq > before)
    verdicts = {n.attrs.get("health") for n in h.nodes_of(NodeKind.CONTAINER)}

    h.record(
        "a container that goes unhealthy is resent",
        "the state does not move when a healthcheck starts failing, so a verdict "
        "outside the hash is a broken container drawn in green, forever",
        moved and "unhealthy" in verdicts,
        f"seq {before} -> {h.store.seq}, verdicts {sorted(v or '-' for v in verdicts)}",
    )


async def scenario_crash_loop_depth(h: Harness) -> None:
    """How deep the loop is, and what it is allowed to cost.

    `RestartCount` is not in `GET /containers/json` at any API version, so
    carrying it means an inspect per container -- which is the change from one
    request per List to one per container that the informer's whole design
    exists to avoid. The answer is to inspect only containers whose listed
    state is `restarting`: bounded, usually empty, and exactly the set for
    which the number means anything.

    Three checks, and the first would pass on its own against an agent that
    inspects everything -- which is why the other two are here. The host is
    given a mixed population first, because "only the restarting one" is not a
    claim a single-container fixture can make.
    """
    steady = _inspected(h)

    h.engine.containers.append(make_container(LOOPING, "shop-worker-1"))
    h.engine.containers.append(make_container(SETTLED, "shop-cache-1"))
    for container in h.engine.containers:
        if container["Id"] != LOOPING:
            continue
        container["State"] = "restarting"
        container["Status"] = "Restarting (137) 2 seconds ago"
        container["RestartCount"] = 417

    await h.engine.emit("container")
    seen = await h.until(
        lambda: any(n.attrs.get("restart_count") == 417 for n in h.nodes_of(NodeKind.CONTAINER))
    )
    depths = {n.attrs.get("restart_count") for n in h.nodes_of(NodeKind.CONTAINER)}

    h.record(
        "a crash loop reports how deep it is",
        "`Unstable` is the same word on the second failure and the four-hundredth; "
        "the count is the only thing that separates a deploy from an incident",
        seen,
        f"depths {sorted(str(d) for d in depths)}",
    )

    # The hashing decision, end to end, and the reason this is a separate
    # check: above, the state moved too, so an agent with the count outside
    # its hash would still have resent the container. Here nothing moves but
    # the number -- which is the case an operator watching a loop deepen is
    # actually in, and the case where "hash it or not" has an answer.
    for container in h.engine.containers:
        if container["Id"] == LOOPING:
            container["RestartCount"] = 418
    await h.engine.emit("container")
    deepened = await h.until(
        lambda: any(n.attrs.get("restart_count") == 418 for n in h.nodes_of(NodeKind.CONTAINER))
    )

    h.record(
        "a loop that deepens is resent",
        "the state stays `restarting` across every restart, so a count outside "
        "the hash is a loop that is reported once and never again",
        deepened,
        "417 -> 418 with no state change",
    )

    h.record(
        "a settled host pays for no inspects",
        "inspecting every container turns one request per List into one per "
        "container, which is the cost the informer exists to avoid",
        steady == set(),
        f"{len(steady)} inspect(s) before anything was restarting",
    )

    inspected = _inspected(h) - steady
    h.record(
        "and inspects only what is looping",
        "the bound is the whole design; on a healthy host it is what makes the "
        "cheap way cheap, and nothing about the answer reveals its absence",
        inspected == {LOOPING},
        f"inspected {sorted(i[:12] for i in inspected) or 'nothing'}",
    )


def _inspected(h: Harness) -> set[str]:
    """Container ids fetched via `GET /containers/{id}/json`.

    Ids rather than a count, because the count is not the claim: an agent that
    inspects twice as often is wasteful, and one that inspects a container
    that is not restarting has lost the bound entirely. Excludes the list
    endpoint, which is `/containers/json` and looks identical to a prefix test.
    """
    return {
        call.split("/")[2]
        for call in h.engine.recorded.calls
        if call.startswith("/containers/")
        and call.endswith("/json")
        and call != "/containers/json"
    }


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


async def scenario_logs(h: Harness) -> None:
    """A log read, which is deliberately not a command.

    `CommandKind` is the closed set of mutations a read-only Controller
    refuses. Logs are their own frame and the agent answers them regardless of
    its own `read_only` setting, because refusing to show an operator why a
    container is failing on the grounds that the platform is in its safe mode
    is exactly backwards.

    The check that cannot be made anywhere else is the framing. Docker
    multiplexes both streams down one connection with an 8-byte header per
    chunk, and an agent that returns the lines but loses the tag passes every
    plain-text fixture while throwing away most of the diagnostic value.
    """
    target = next(iter(h.nodes_of(NodeKind.CONTAINER)))
    container_id = target.urn.segments[-1]
    h.engine.logs[container_id] = [
        (False, "listening on :80"),
        (True, "upstream timed out"),
        (False, "shutting down"),
    ]

    provider = h.collector.agent_provider(target.source, create=False)
    if provider is None:
        h.record("logs round trip", "the host must have a provider to read through", False)
        return

    result = await provider.logs(container_id, 100)
    h.record(
        "logs round trip",
        "a logs request must reach the socket and its answer must find its way back",
        result.ok and len(result.lines) == 3,
        result.reason or f"{len(result.lines)} lines",
    )

    tagged = [(line.stderr, line.text) for line in result.lines]
    h.record(
        "log streams stay tagged",
        "the line that explains a crash is almost always the one on stderr",
        (True, "upstream timed out") in tagged
        and (False, "listening on :80") in tagged,
        f"{sum(1 for stderr, _ in tagged if stderr)} of {len(tagged)} on stderr",
    )

    tailed = await provider.logs(container_id, 1)
    h.record(
        "tail is applied by the daemon",
        "an unbounded read is how a month-old log exhausts a small agent",
        tailed.ok and len(tailed.lines) < 3,
        f"{len(tailed.lines)} lines for tail=1",
    )

    missing = await provider.logs("0" * 64, 100)
    h.record(
        "a refusal says why",
        "an operator must not have to guess whether the container or the request was wrong",
        not missing.ok and bool(missing.reason),
        missing.reason or "refused with no reason",
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


async def scenario_budget(h: Harness) -> None:
    """The numbers in ARCHITECTURE §11, measured rather than remembered.

    A budget that is only written down has already been exceeded. This is the
    check that noticed mutual TLS doubling the binary — which was fine, and
    which nobody would have known was fine without measuring it.

    Runs last, because it loads the engine to the hundred containers the
    published figures are quoted for and leaves it that way.
    """
    if h.pid is None or _rss_mib(h.pid) is None:
        h.record(
            "agent budget",
            "the budget is the whole argument for the agent's design",
            True,
            "skipped: no /proc on this platform",
        )
        return

    size = h.binary.stat().st_size / MIB
    h.record(
        "binary within budget",
        f"the artifact is copied onto every managed host; budget {BUDGET_BINARY_MIB} MiB",
        size < BUDGET_BINARY_MIB,
        f"{size:.2f} MiB",
    )

    for index in range(100):
        h.engine.containers.append(make_container(f"{index:064x}", f"budget-{index}"))
    await h.engine.emit("container")
    await h.until(lambda: len(h.nodes_of(NodeKind.CONTAINER)) >= 100, within=15.0)

    rss = _rss_mib(h.pid)
    h.record(
        "RSS within budget",
        f"multiplied by the fleet, on hardware bought for something else; "
        f"budget {BUDGET_RSS_MIB} MiB",
        rss is not None and rss < BUDGET_RSS_MIB,
        f"{rss:.2f} MiB with {len(h.nodes_of(NodeKind.CONTAINER))} containers"
        if rss is not None
        else "unreadable",
    )

    # Measured over a window with nothing happening, which is where the agent
    # spends almost all of its life. A polling loop that looks free at one
    # sample does not survive three seconds of arithmetic.
    before = _cpu_seconds(h.pid)
    await asyncio.sleep(IDLE_WINDOW)
    used = _cpu_seconds(h.pid)
    idle = None if before is None or used is None else (used - before) / IDLE_WINDOW * 100
    h.record(
        "idle CPU within budget",
        f"an idle agent must cost nothing measurable; budget {BUDGET_CPU_PERCENT} %",
        idle is not None and idle < BUDGET_CPU_PERCENT,
        f"{idle:.2f} % over {IDLE_WINDOW:.0f}s" if idle is not None else "unreadable",
    )


#: ADR-0010's budget, quoted as written. ARCHITECTURE §11 carries the measured
#: figures; these are the ceilings they must stay under.
BUDGET_BINARY_MIB = 12.0
BUDGET_RSS_MIB = 20.0
BUDGET_CPU_PERCENT = 0.1

MIB = 1024 * 1024

#: Long enough that a busy-wait cannot hide in the rounding, short enough not
#: to dominate the run.
IDLE_WINDOW = 3.0


def _rss_mib(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def _cpu_seconds(pid: int) -> float | None:
    """User + system time, from `/proc/<pid>/stat`.

    Split on `") "` rather than on whitespace: the second field is the
    executable name in parentheses and may contain spaces, which is the
    classic way a naive parser reads this file wrong.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


SCENARIOS: list[tuple[str, Callable[[Harness], Awaitable[None]]]] = [
    ("initial sync", scenario_initial_sync),
    ("watch", scenario_watch),
    ("status hash", scenario_status_string_is_not_hashed),
    ("health hash", scenario_health_is_hashed),
    ("steady state", scenario_steady_state_is_silent),
    ("coalescing", scenario_coalescing),
    ("removal", scenario_removal),
    ("commands", scenario_command),
    # After the command scenario, because it is the first thing that puts a
    # container into `restarting` and the inspect-budget check reads "how many
    # inspects has this agent made so far" as its baseline.
    ("crash loop depth", scenario_crash_loop_depth),
    ("logs", scenario_logs),
    ("reconnect", scenario_reconnect),
    ("budget", scenario_budget),
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

    controller = await conformance_controller.start(workdir, port)

    print(f"engine socket : {socket_path}")
    print(f"controller    : {controller.url} (mutual TLS)")
    print(f"agent         : {agent_binary}\n")

    agent = await asyncio.create_subprocess_exec(
        *controller.agent_args(agent_binary, socket_path, "solo"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "RUST_BACKTRACE": "1"},
    )

    harness = Harness(engine, controller.app, port, binary=agent_binary, pid=agent.pid)
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
        await controller.stop()
        await engine.stop()

    return report("conformance", harness.checks)


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
