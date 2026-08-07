"""Fleet conformance: does the architecture hold with more than one agent?

    python -m bystack.conformance.fleet ../agent/target/release/bystack-agent

`runner.py` proves one agent behaves. That is the component contract, and it
is deliberately blind to everything ADR-0008 actually claims about a *fleet*:

* one partition per host, and an agent structurally unable to name another's
  (ARCHITECTURE §6 -- a security control since the pivot, not hygiene);
* logical identity scoped by engine, so two hosts running a `shop` stack are
  two stacks rather than one flapping between them (§4);
* image digests correlating across hosts *for free*, which is the one place
  the partition rule and the correlation rule pull in opposite directions;
* one agent's disconnect degrading one host and nothing else (§2, MIGRATION §2);
* commands reaching the host they were addressed to and no other (§9).

Every one of those is a property of the fleet, not of an agent, so no
single-agent run can observe it -- and each fails silently: the graph looks
plausible, and it is describing the wrong machine.

Three real agent binaries, three scripted engines, one Controller.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn

from bystack.agent.v1 import agent_pb2 as wire
from bystack.api.app import API_PREFIX, create_app
from bystack.config import AgentsConfig, Settings
from bystack.conformance.engine import ScriptedEngine, make_container
from bystack.core.graph.model import EdgeKind
from bystack.core.identity import NodeKind, engine_scope, image_urn
from bystack.core.ports.command import CommandKind, CommandRejected, CommandRequest
from bystack.core.ports.provider import ProviderState

SETTLE = 8.0

#: One image digest deliberately shared by two hosts.
#:
#: Image identity is the content digest and is *not* engine-scoped, because
#: correlating the same image across a fleet for free is the whole point
#: (ARCHITECTURE §4). That makes it the only URN two partitions legitimately
#: both contain, and therefore the only place the partition rule can be got
#: wrong in a way that looks like correlation working.
SHARED_DIGEST = "sha256:" + "a" * 64
GAMMA_DIGEST = "sha256:" + "e" * 64


def _cid(seed: str) -> str:
    return (seed * 64)[:64]


@dataclass
class Host:
    """One managed machine: a scripted engine plus the agent watching it."""

    name: str
    engine_id: str
    containers: list[dict[str, Any]]
    image_digest: str
    read_only: bool = False

    engine: ScriptedEngine | None = None
    process: asyncio.subprocess.Process | None = None
    socket: Path | None = None
    output: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        """The partition key: the engine id, normalized exactly once."""
        return engine_scope(self.engine_id)

    def info(self) -> dict[str, Any]:
        return {
            "ID": self.engine_id,
            "Name": self.name,
            "ServerVersion": "29.6.0",
            "OperatingSystem": "Fedora Linux 44",
            "KernelVersion": "6.19.10",
            "Architecture": "x86_64",
            "NCPU": 8,
            "MemTotal": 16_000_000_000,
            "ContainersRunning": len(self.containers),
            "Containers": len(self.containers),
        }

    def network(self) -> dict[str, Any]:
        return {
            "Id": f"net-{self.name}", "Name": "shop_default", "Driver": "bridge",
            "Scope": "local", "Internal": False, "Attachable": False, "Ingress": False,
            "Labels": {}, "IPAM": {"Config": [{"Subnet": "172.24.0.0/16"}]},
        }

    def volume(self) -> dict[str, Any]:
        return {
            "Name": "shop_data", "Driver": "local",
            "Mountpoint": "/var/lib/docker/volumes/shop_data/_data", "Scope": "local",
            "CreatedAt": "2026-01-01T00:00:00Z", "Labels": {},
        }

    def image(self) -> dict[str, Any]:
        return {
            "Id": self.image_digest, "RepoTags": ["nginx:latest"],
            "RepoDigests": ["nginx@sha256:" + "b" * 64], "Size": 142_000_000,
            "Created": 1_699_000_000, "Labels": {},
        }


def fleet() -> list[Host]:
    """Three hosts, chosen so the interesting cases are unavoidable.

    * **alpha** carries a pre-25.0 colon-delimited engine id. A mixed-version
      fleet is the normal case in the home labs this targets, and the id has
      two spellings -- one for URNs, one for `node.source` -- which is a bug
      that only appears when something compares them.
    * **alpha** and **beta** both run a `shop` stack with a `web` service and
      **the same image digest**. Same names, different hosts: the stacks and
      services must stay distinct while the image must not.
    * **gamma** is read-only, so the second enforcement point (§9) is exercised
      by an agent that refuses rather than by a Controller that never asks.
    """
    return [
        Host(
            name="alpha",
            engine_id="ALPH:A2B3:C4D5:E6F7",
            image_digest=SHARED_DIGEST,
            containers=[
                make_container(_cid("a"), "shop-web-1", service="web",
                               image_id=SHARED_DIGEST, network_id="net-alpha"),
                make_container(_cid("b"), "shop-api-1", service="api",
                               image_id=SHARED_DIGEST, network_id="net-alpha"),
            ],
        ),
        Host(
            name="beta",
            engine_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
            image_digest=SHARED_DIGEST,
            containers=[
                make_container(_cid("c"), "shop-web-1", service="web",
                               image_id=SHARED_DIGEST, network_id="net-beta"),
                make_container(_cid("d"), "shop-db-1", service="db",
                               image_id=SHARED_DIGEST, network_id="net-beta"),
                make_container(_cid("f"), "loose-1", project=None, service=None,
                               image_id=SHARED_DIGEST, network_id="net-beta"),
            ],
        ),
        Host(
            name="gamma",
            engine_id="3f2b1c0a-1111-2222-3333-444455556666",
            image_digest=GAMMA_DIGEST,
            read_only=True,
            containers=[
                make_container(_cid("9"), "edge-proxy-1", project="edge", service="proxy",
                               image_id=GAMMA_DIGEST, network_id="net-gamma"),
            ],
        ),
    ]


@dataclass
class Check:
    name: str
    why: str
    passed: bool
    detail: str = ""


class Harness:
    def __init__(self, hosts: list[Host], app: Any, url: str) -> None:
        self.hosts = {h.name: h for h in hosts}
        self.app = app
        self.url = url
        self.checks: list[Check] = []

    @property
    def store(self) -> Any:
        return self.app.state.context.store

    @property
    def collector(self) -> Any:
        return self.app.state.context.collector

    def nodes(self, kind: str | None = None, source: str | None = None) -> list[Any]:
        return [
            n
            for n in self.store.snapshot().nodes
            if (kind is None or n.kind == kind) and (source is None or n.source == source)
        ]

    def edges(self, kind: str | None = None) -> list[Any]:
        return [e for e in self.store.snapshot().edges if kind is None or e.kind == kind]

    def containers_on(self, host: Host) -> list[Any]:
        return self.nodes(NodeKind.CONTAINER, host.source)

    def provider(self, host: Host) -> Any:
        return self.collector.providers.get(host.source)

    def state(self, host: Host) -> ProviderState | None:
        provider = self.provider(host)
        return provider.health().state if provider is not None else None

    async def until(self, predicate: Callable[[], bool], within: float = SETTLE) -> bool:
        deadline = asyncio.get_running_loop().time() + within
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return predicate()

    def record(self, name: str, why: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, why, passed, detail))

    # -- driving agents ----------------------------------------------------

    async def spawn(self, host: Host) -> None:
        args = [str(AGENT_BINARY), "--controller", self.url, "--socket", str(host.socket)]
        if host.read_only:
            args.append("--read-only")
        host.process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "RUST_BACKTRACE": "1"},
        )

    async def kill(self, host: Host) -> None:
        process = host.process
        if process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            output = await asyncio.wait_for(process.stdout.read(), timeout=2.0)  # type: ignore[union-attr]
            if output:
                host.output.append(output.decode())
        await process.wait()
        host.process = None


AGENT_BINARY: Path = Path()


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


async def scenario_convergence(h: Harness) -> None:
    """Three agents dial in; three partitions appear. Nothing is configured."""
    expected = {host.source for host in h.hosts.values()}

    converged = await h.until(
        lambda: expected <= set(h.collector.providers), within=15.0
    )
    h.record(
        "the fleet converges",
        "agents arrive rather than being reached; the provider set is runtime, not config",
        converged,
        f"{len(h.collector.providers)}/{len(expected)} providers registered",
    )

    ready = await h.until(
        lambda: all(h.state(host) is ProviderState.READY for host in h.hosts.values()),
        within=15.0,
    )
    h.record(
        "every agent reaches READY",
        "a stream that never leaves SYNCING is a host the operator cannot trust",
        ready,
        ", ".join(f"{n}={h.state(host)}" for n, host in h.hosts.items()),
    )

    hosts_in_graph = {n.source for n in h.nodes(NodeKind.HOST)}
    h.record(
        "one host node per machine",
        "the host node is written from Hello, before any slice arrives",
        hosts_in_graph == expected,
        f"{len(hosts_in_graph)} host node(s)",
    )


async def scenario_partitioning(h: Harness) -> None:
    """Every node belongs to exactly the host that reported it."""
    counts = {}
    ok = True
    for name, host in h.hosts.items():

        def landed(host: Host = host) -> bool:
            return len(h.containers_on(host)) == len(host.containers)

        reached = await h.until(landed)
        counts[name] = f"{len(h.containers_on(host))}/{len(host.containers)}"
        ok = ok and reached

    h.record(
        "containers land in their own partition",
        "an agent writes through a writer bound to one partition and structurally to no other",
        ok,
        " ".join(f"{n}:{c}" for n, c in counts.items()),
    )

    # Nothing may sit in a partition no agent owns. A source that is not an
    # enrolled engine id means something wrote outside the registry.
    known = {host.source for host in h.hosts.values()}
    strays = {n.source for n in h.store.snapshot().nodes} - known
    h.record(
        "no node outside an enrolled partition",
        "a partition key that belongs to no agent is a write that escaped the registry",
        not strays,
        f"stray sources: {sorted(strays)}" if strays else "",
    )


async def scenario_engine_id_spelling(h: Harness) -> None:
    """The pre-25.0 host must have exactly one identity, not two.

    `engine_scope()` strips the colons for URNs. Normalizing in one place and
    not the other gives the host one spelling in `node.source` and another in
    every URN it appears in -- and `/graph?sources=` then matches neither.
    """
    alpha = h.hosts["alpha"]
    assert ":" in alpha.engine_id, "this check is pointless without a colon format"

    nodes = h.containers_on(alpha)
    consistent = bool(nodes) and all(
        ":" not in node.source and node.urn.startswith(f"bystack:container:{alpha.source}/")
        for node in nodes
    )
    h.record(
        "the engine id has one spelling",
        "normalizing for URNs but not for the partition key gives one host two identities",
        consistent,
        f"source={nodes[0].source if nodes else '<none>'}",
    )


async def scenario_logical_scoping(h: Harness) -> None:
    """Two hosts, both running a `shop` stack with a `web` service."""
    alpha, beta = h.hosts["alpha"], h.hosts["beta"]

    stacks = {n.urn for n in h.nodes(NodeKind.STACK)}
    distinct = len({n.urn for n in h.nodes(NodeKind.STACK, alpha.source)} & {
        n.urn for n in h.nodes(NodeKind.STACK, beta.source)
    }) == 0
    h.record(
        "same stack name on two hosts is two stacks",
        "logical URNs are engine-scoped; collapsing them makes one host's redeploy flap the other",
        distinct and len(stacks) >= 3,
        f"{len(stacks)} stack(s) across the fleet",
    )

    services = {n.urn for n in h.nodes(NodeKind.SERVICE)}
    web = {urn for urn in services if urn.endswith("/shop/web")}
    h.record(
        "a service is scoped to its engine",
        "two `shop/web` services on two hosts must not correlate into one",
        len(web) == 2,
        f"{len(web)} distinct shop/web service(s)",
    )


async def scenario_image_correlation(h: Harness) -> None:
    """The one URN two partitions legitimately share."""
    alpha, beta = h.hosts["alpha"], h.hosts["beta"]
    shared = image_urn(SHARED_DIGEST)

    present = await h.until(lambda: h.store.node(shared) is not None)
    h.record(
        "an image digest correlates across hosts",
        "image identity is the content digest and is not engine-scoped -- correlation for free",
        present,
        f"{len(h.nodes(NodeKind.IMAGE))} image node(s) for 3 hosts",
    )

    users = {
        e.src.split(":")[2].split("/")[0]
        for e in h.edges(EdgeKind.USES_IMAGE)
        if e.dst == shared
    }
    h.record(
        "both hosts point at the shared image",
        "correlation that only one side declares is not correlation",
        {alpha.source, beta.source} <= users,
        f"referenced from {len(users)} host(s)",
    )


async def scenario_isolation_on_removal(h: Harness) -> None:
    """alpha loses everything. beta and gamma must not notice."""
    alpha, beta, gamma = h.hosts["alpha"], h.hosts["beta"], h.hosts["gamma"]
    before = {
        "beta": len(h.containers_on(beta)),
        "gamma": len(h.containers_on(gamma)),
    }

    assert alpha.engine is not None
    alpha.engine.containers = []
    await alpha.engine.emit("container")

    emptied = await h.until(lambda: not h.containers_on(alpha))
    h.record(
        "a host emptying itself is reported",
        "membership carries deletion; an empty id set means an empty host",
        emptied,
        f"alpha: {len(h.containers_on(alpha))} container(s)",
    )

    after = {
        "beta": len(h.containers_on(beta)),
        "gamma": len(h.containers_on(gamma)),
    }
    h.record(
        "one host's reconcile cannot empty another",
        "a compromised agent may lie about its own host and nothing else (ARCHITECTURE §6)",
        after == before,
        f"beta {before['beta']}->{after['beta']}, gamma {before['gamma']}->{after['gamma']}",
    )

    # Put alpha back; later scenarios read a live fleet.
    alpha.engine.containers = list(alpha.containers)
    await alpha.engine.emit("container")
    await h.until(lambda: len(h.containers_on(alpha)) == len(alpha.containers))


async def scenario_shared_image_ownership(h: Harness) -> None:
    """alpha stops carrying the shared image. beta still runs it.

    The image slice is the one place a kind-scoped reconcile in one partition
    can reach a node another partition also owns, because the digest is
    global by design. If alpha's reconcile takes the node -- or the edges
    incident to it -- beta's containers are left pointing at nothing, and the
    UI draws a host whose image vanished while it is still running it.
    """
    alpha, beta = h.hosts["alpha"], h.hosts["beta"]
    shared = image_urn(SHARED_DIGEST)

    beta_edges_before = len([
        e for e in h.edges(EdgeKind.USES_IMAGE)
        if e.dst == shared and e.src.startswith(f"bystack:container:{beta.source}/")
    ])

    assert alpha.engine is not None
    alpha.engine.images = []
    await alpha.engine.emit("image")
    await asyncio.sleep(2.0)

    beta_edges_after = len([
        e for e in h.edges(EdgeKind.USES_IMAGE)
        if e.dst == shared and e.src.startswith(f"bystack:container:{beta.source}/")
    ])

    h.record(
        "a shared image survives one host dropping it",
        "beta is still running the image; alpha's partition does not get to decide that",
        h.store.node(shared) is not None,
        "image node present" if h.store.node(shared) is not None else "image node deleted",
    )
    h.record(
        "one host dropping an image cannot cut another's edges",
        "an edge is owned by the container that declares it, and beta declares these",
        beta_edges_after == beta_edges_before,
        f"beta uses_image edges {beta_edges_before} -> {beta_edges_after}",
    )


async def scenario_partial_outage(h: Harness) -> None:
    """Kill beta's agent. beta degrades; the rest of the fleet does not."""
    alpha, beta, gamma = h.hosts["alpha"], h.hosts["beta"], h.hosts["gamma"]
    kept = len(h.containers_on(beta))

    await h.kill(beta)

    degraded = await h.until(lambda: h.state(beta) is ProviderState.DEGRADED, within=15.0)
    h.record(
        "a lost agent degrades its host",
        "DEGRADED means stale but not wrong -- the last known topology beats a blank screen",
        degraded,
        f"beta={h.state(beta)}",
    )
    h.record(
        "a degraded host keeps its graph",
        "blanking a host because its laptop closed its lid loses the operator's whole picture",
        len(h.containers_on(beta)) == kept and kept > 0,
        f"beta retains {len(h.containers_on(beta))} container(s)",
    )
    h.record(
        "the rest of the fleet is untouched",
        "one agent's disconnect is one host's problem",
        h.state(alpha) is ProviderState.READY and h.state(gamma) is ProviderState.READY,
        f"alpha={h.state(alpha)}, gamma={h.state(gamma)}",
    )

    # A command to a host with no agent must be answered now, not left to
    # burn the full 45s deadline waiting for a reply that provably cannot
    # arrive -- `_detach` fails the waiters before it drops the session.
    target = next(iter(h.containers_on(beta)))
    detail = ""
    refused = False
    try:
        result = await asyncio.wait_for(
            h.app.state.context.commands.execute(
                CommandRequest(kind=CommandKind.STOP, target=target.urn)
            ),
            timeout=5.0,
        )
        refused = not result.ok
        detail = f"{result.status}: {result.outcomes[0].detail}"
    except CommandRejected as exc:
        refused = True
        detail = str(exc.reason)
    except TimeoutError:
        detail = "no answer within 5s"
    h.record(
        "a disconnected host refuses commands immediately",
        "waiting out a 45s deadline for a reply that provably cannot arrive is not a timeout",
        refused,
        detail,
    )


async def scenario_no_resurrection(h: Harness) -> None:
    """The gap is where a stale payload cache resurrects the dead.

    While beta is down its engine loses a container. On reconnect the agent
    sends a full Sync. A Controller that kept the previous connection's
    membership cache applies the first frame against a picture from before the
    gap, and the container comes back -- silently, and forever.
    """
    beta = h.hosts["beta"]
    assert beta.engine is not None
    gone = _cid("d")
    beta.engine.containers = [c for c in beta.engine.containers if c["Id"] != gone]

    await h.spawn(beta)
    back = await h.until(lambda: h.state(beta) is ProviderState.READY, within=20.0)
    h.record(
        "a killed agent reconnects and re-syncs",
        "reconnect begins with a full Sync; there is no backlog to replay",
        back,
        f"beta={h.state(beta)}",
    )

    settled = await h.until(
        lambda: len(h.containers_on(beta)) == len(beta.engine.containers)  # type: ignore[union-attr]
    )
    resurrected = any(node.urn.endswith(f"/{gone}") for node in h.containers_on(beta))
    h.record(
        "nothing removed during the gap comes back",
        "keeping the payload cache across a disconnect resurrects what died while away",
        settled and not resurrected,
        f"beta: {len(h.containers_on(beta))} container(s), "
        f"{'resurrected' if resurrected else 'no resurrection'}",
    )


async def scenario_command_routing(h: Harness) -> None:
    """A command must reach the host it names, and only that host."""
    alpha, beta, gamma = h.hosts["alpha"], h.hosts["beta"], h.hosts["gamma"]
    service = h.app.state.context.commands

    for host in (alpha, beta, gamma):
        assert host.engine is not None
        host.engine.recorded.actions.clear()

    target = next(iter(h.containers_on(beta)))
    result = await service.execute(
        CommandRequest(kind=CommandKind.STOP, target=target.urn, reason="fleet conformance")
    )
    h.record(
        "a command round-trips through the right agent",
        "the command channel correlates results per session, not per Controller",
        result.ok,
        f"{result.status}",
    )

    assert beta.engine is not None and alpha.engine is not None and gamma.engine is not None
    hit_beta = any(cid == target.urn.segments[-1] for cid, _, _ in beta.engine.recorded.actions)
    quiet = not alpha.engine.recorded.actions and not gamma.engine.recorded.actions
    h.record(
        "only the addressed host is touched",
        "a fan-out that reaches the wrong daemon stops containers nobody asked about",
        hit_beta and quiet,
        f"alpha={len(alpha.engine.recorded.actions)}, "
        f"beta={len(beta.engine.recorded.actions)}, "
        f"gamma={len(gamma.engine.recorded.actions)} action(s)",
    )


async def scenario_logical_fanout(h: Harness) -> None:
    """Restarting a *service* must expand to its containers on that host only."""
    alpha = h.hosts["alpha"]
    service = h.app.state.context.commands

    for host in h.hosts.values():
        assert host.engine is not None
        host.engine.recorded.actions.clear()

    stack = next(
        (n for n in h.nodes(NodeKind.STACK, alpha.source)), None
    )
    if stack is None:
        h.record("a stack fans out to its own host", "no stack found on alpha", False)
        return

    result = await service.execute(
        CommandRequest(kind=CommandKind.RESTART, target=stack.urn, reason="fleet conformance")
    )
    assert alpha.engine is not None
    touched = {cid for cid, _, _ in alpha.engine.recorded.actions}
    elsewhere = 0
    for host in h.hosts.values():
        if host is not alpha and host.engine is not None:
            elsewhere += len(host.engine.recorded.actions)
    h.record(
        "a stack fans out to its own host only",
        "the Controller expands logical targets by walking contains/realized_by, per partition",
        result.ok and len(touched) == len(alpha.containers) and elsewhere == 0,
        f"{len(touched)} container(s) on alpha, {elsewhere} elsewhere, "
        f"{len(result.outcomes)} outcome(s)",
    )


async def scenario_read_only_agent(h: Harness) -> None:
    """gamma's agent refuses regardless of what arrives on the wire.

    The Controller here is *not* read-only -- it dispatched two commands
    already. This is the second choke point (ARCHITECTURE §9): the agent holds
    root-equivalent access to a machine and "the Controller said so" is not an
    acceptable sole justification for using it.
    """
    gamma = h.hosts["gamma"]
    assert gamma.engine is not None
    gamma.engine.recorded.actions.clear()

    target = next(iter(h.containers_on(gamma)), None)
    if target is None:
        h.record("a read-only agent refuses", "no container on gamma", False)
        return

    refused = False
    detail = ""
    try:
        result = await h.app.state.context.commands.execute(
            CommandRequest(kind=CommandKind.STOP, target=target.urn)
        )
        refused = not result.ok
        detail = f"{result.status}: {result.outcomes[0].detail}"
    except CommandRejected as exc:
        refused = True
        detail = str(exc.reason)

    h.record(
        "a read-only host refuses the mutation",
        "an agent advertises read_only at Hello and the Controller must honour it",
        refused,
        detail,
    )
    h.record(
        "the refusal never reached the socket",
        "an agent that refuses after acting has refused nothing",
        not gamma.engine.recorded.actions,
        f"{len(gamma.engine.recorded.actions)} action(s) on gamma's engine",
    )

    provider = h.provider(gamma)
    h.record(
        "a read-only host advertises it",
        "the UI must disable the actions rather than offer them and fail",
        provider is not None
        and not provider.supported_commands(h.store.node(target.urn)),
        "no commands advertised" if provider is not None else "no provider",
    )

    # The check above only proves the *Controller* honoured the advertisement,
    # because it refuses before a frame is ever built. ARCHITECTURE §9 claims
    # something stronger and separate: the agent holds root-equivalent access
    # to a machine, and "the Controller said so" is not an acceptable sole
    # justification for using it. So put the frame on the wire anyway -- which
    # is what a compromised or buggy Controller does -- and require the socket
    # to stay untouched.
    session = provider._session if provider is not None else None
    if session is None:
        h.record("the agent refuses on its own authority", "gamma has no session", False)
        return

    await session.send(
        wire.Envelope(
            command=wire.Command(
                command_id="fleet-conformance-bypass",
                verb="stop",
                target_id=target.urn.segments[-1],
            )
        )
    )
    await asyncio.sleep(2.0)

    h.record(
        "the agent refuses on its own authority",
        "read-only is enforced at both choke points; the second is the one holding the socket",
        not gamma.engine.recorded.actions,
        f"{len(gamma.engine.recorded.actions)} action(s) after a command sent past the Controller",
    )


SCENARIOS: list[tuple[str, Callable[[Harness], Awaitable[None]]]] = [
    ("convergence", scenario_convergence),
    ("partitioning", scenario_partitioning),
    ("engine id", scenario_engine_id_spelling),
    ("logical scoping", scenario_logical_scoping),
    ("image correlation", scenario_image_correlation),
    ("isolation", scenario_isolation_on_removal),
    ("shared image", scenario_shared_image_ownership),
    ("partial outage", scenario_partial_outage),
    ("no resurrection", scenario_no_resurrection),
    ("command routing", scenario_command_routing),
    ("logical fan-out", scenario_logical_fanout),
    ("read-only agent", scenario_read_only_agent),
]


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


async def run(binary: Path, port: int) -> int:
    global AGENT_BINARY
    AGENT_BINARY = binary

    workdir = Path(tempfile.mkdtemp(prefix="bystack-fleet-"))
    hosts = fleet()

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
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.05)

    url = f"ws://127.0.0.1:{port}{API_PREFIX}/agents/connect"
    harness = Harness(hosts, app, url)

    print(f"controller : {url}")
    print(f"agent      : {binary}")
    for host in hosts:
        host.socket = workdir / f"{host.name}.sock"
        host.engine = ScriptedEngine(
            host.socket,
            info=host.info(),
            containers=list(host.containers),
            networks=[host.network()],
            volumes=[host.volume()],
            images=[host.image()],
        )
        await host.engine.start()
        await harness.spawn(host)
        flags = " (read-only)" if host.read_only else ""
        print(f"  {host.name:<6} engine {host.engine_id} -> {host.socket}{flags}")
    print()

    try:
        for name, scenario in SCENARIOS:
            dead = [h.name for h in hosts if h.process is not None and h.process.returncode]
            if dead:
                harness.record(name, "an agent exited", False, f"dead: {', '.join(dead)}")
                continue
            await scenario(harness)
    finally:
        for host in hosts:
            await harness.kill(host)
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serving, timeout=5.0)
        for host in hosts:
            if host.engine is not None:
                await host.engine.stop()

        printed = [(h.name, "".join(h.output).rstrip()) for h in hosts]
        if any(text for _, text in printed):
            print("--- agent output ---")
            for name, text in printed:
                for line in text.splitlines():
                    print(f"  [{name}] {line}")
            print()

    return report(harness)


def report(harness: Harness) -> int:
    print("--- fleet conformance ---")
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
    parser.add_argument("--port", type=int, default=8131)
    args = parser.parse_args(argv)

    if not args.agent.exists():
        print(f"no such agent binary: {args.agent}", file=sys.stderr)
        return 2

    return asyncio.run(run(args.agent, args.port))


if __name__ == "__main__":
    raise SystemExit(main())
