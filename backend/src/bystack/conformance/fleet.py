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
* commands reaching the host they were addressed to and no other (§9);
* log reads doing the same, over a path that shares none of the command
  machinery -- and failing plausibly rather than loudly when they do not.

Every one of those is a property of the fleet, not of an agent, so no
single-agent run can observe it -- and each fails silently: the graph looks
plausible, and it is describing the wrong machine.

Three real agent binaries, three scripted engines, one Controller.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import platform
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from bystack.agent.v1 import agent_pb2 as wire
from bystack.conformance import controller as conformance_controller
from bystack.conformance.engine import ScriptedEngine, make_container
from bystack.conformance.report import Check, report
from bystack.core.graph.model import EdgeKind
from bystack.core.identity import NodeKind, engine_scope, image_urn
from bystack.core.ports.command import CommandKind, CommandRejected, CommandRequest
from bystack.core.ports.provider import ProviderState
from bystack.infra import releases
from bystack.infra.releases import Release, ReleaseStore

SETTLE = 8.0

#: The private half of a key the agent under test was built to trust.
#:
#: Supplied by the operator running this, because it cannot be derived: the
#: public key is *compiled into* the binary (ADR-0017), so a harness that
#: generated its own could only ever demonstrate the refusal path. Absent, the
#: upgrade scenario skips and says so.
SIGNING_KEY: ed25519.Ed25519PrivateKey | None = None

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


class Harness:
    def __init__(self, hosts: list[Host], controller: conformance_controller.Controller) -> None:
        self.hosts = {h.name: h for h in hosts}
        self.controller = controller
        self.app = controller.app
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
        assert host.socket is not None, "the scripted engine must be started first"
        # A fresh join token and a fresh state directory per spawn. That is
        # not ceremony for the harness's sake: an agent restarted here goes
        # through enrollment again, which is the path a reinstalled agent
        # takes, and it must land back on the same partition because the
        # engine id is the identity (ADR-0002).
        args = self.controller.agent_args(AGENT_BINARY, host.socket, host.name)
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


async def scenario_logs_routing(h: Harness) -> None:
    """A log read must be answered by the host that holds the container.

    The same property as `scenario_command_routing` over a completely
    different code path: logs are not a `CommandKind`, do not go through
    `CommandService`, and are answered by a read-only Controller — so nothing
    the command scenario proves carries over to them.

    Getting this wrong is worse than an error. A misrouted command fails
    loudly on a daemon that does not have the container; a misrouted log read
    hands the operator another machine's output under the container they
    clicked, which is a plausible answer to the wrong question and the hardest
    kind of wrong to notice during an incident.
    """
    alpha, beta = h.hosts["alpha"], h.hosts["beta"]
    assert alpha.engine is not None and beta.engine is not None

    # Distinguishable per host, so an answer names the machine it came from
    # rather than merely being the right shape.
    for host in (alpha, beta):
        assert host.engine is not None
        for container in host.containers:
            host.engine.logs[container["Id"]] = [
                (False, f"serving from {host.name}"),
                (True, f"{host.name}: upstream timed out"),
            ]

    target = next(iter(h.containers_on(beta)))
    container_id = target.urn.segments[-1]

    for host in h.hosts.values():
        assert host.engine is not None
        host.engine.recorded.calls.clear()

    answer = await h.provider(beta).logs(container_id, 100)
    text = " ".join(line.text for line in answer.lines)
    asked = {
        name: sum(1 for call in host.engine.recorded.calls if call.endswith("/logs"))
        for name, host in h.hosts.items()
        if host.engine is not None
    }

    h.record(
        "a log read is answered by the host that holds the container",
        "logs bypass CommandService entirely, so the routing the command scenario "
        "pins says nothing about them",
        answer.ok and "beta" in text and "alpha" not in text,
        answer.reason or f"{len(answer.lines)} line(s): {text[:60]!r}",
    )

    h.record(
        "and no other host's daemon is asked",
        "a read that fans out costs bandwidth on every uplink in the fleet to "
        "answer a question about one container",
        asked.get("beta") == 1 and asked.get("alpha") == 0 and asked.get("gamma") == 0,
        ", ".join(f"{name}={count}" for name, count in sorted(asked.items())),
    )

    # The partition rule, for reads. `PartitionWriter` stops an agent writing
    # another host's entities; this is the same line drawn from the other
    # direction -- one agent must not be usable as a window onto a machine it
    # does not run on.
    for host in h.hosts.values():
        assert host.engine is not None
        host.engine.recorded.calls.clear()

    crossed = await h.provider(alpha).logs(container_id, 100)
    beta_untouched = not any(call.endswith("/logs") for call in beta.engine.recorded.calls)

    h.record(
        "one host's agent cannot read another's container",
        "an agent that answered for an id it does not own would make every "
        "enrolled host a window onto every other one",
        not crossed.ok and beta_untouched,
        crossed.reason or f"answered with {len(crossed.lines)} line(s)",
    )


async def _await_line(stream: Any, matches: Callable[[Any], bool]) -> Any | None:
    """The next event satisfying ``matches``, or ``None`` within the budget.

    ``None`` rather than a raised ``TimeoutError``, for the reason the copy in
    `runner.py` gives: a stream that opens and then says nothing is exactly the
    failure these checks exist to catch, so it must arrive as a failed check
    with the other checks still running, not as a traceback that ends the run.
    """
    deadline = asyncio.get_running_loop().time() + SETTLE
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            event = await asyncio.wait_for(stream.next(), timeout=remaining)
        except TimeoutError:
            return None
        if matches(event):
            return event
        if event.done:
            return None
    return None


async def scenario_live_logs_routing(h: Harness) -> None:
    """A *live* tail must be answered by the host that holds the container.

    `scenario_logs_routing` above pins this for the one-shot read, and none of
    it carries over. A subscription is a different frame, a different lifetime
    and — the part that matters here — a different thing to get wrong: the
    one-shot read is routed once and answered once, while a tail routes every
    chunk that arrives for as long as it is open, through a correlation map
    shared by every subscription on that connection.

    So the misrouting this looks for is worse than the one-shot version. That
    one hands the operator another machine's output once. This one can attach
    another machine's output to a panel that is already open and already
    trusted, line by line, while they watch it.
    """
    alpha, beta = h.hosts["alpha"], h.hosts["beta"]
    assert alpha.engine is not None and beta.engine is not None

    for host in (alpha, beta):
        assert host.engine is not None
        for container in host.containers:
            host.engine.logs[container["Id"]] = [(False, f"serving from {host.name}")]

    target = next(iter(h.containers_on(beta)))
    container_id = target.urn.segments[-1]

    async with h.provider(beta).follow(container_id, 100) as stream:
        first = await _await_line(stream, lambda e: bool(e.lines))
        text = " ".join(line.text for line in first.lines) if first else ""

        h.record(
            "a live tail is answered by the host that holds the container",
            "a tail routes every chunk for as long as it is open, so a "
            "misrouting here feeds another machine's output into a panel the "
            "operator is already watching",
            first is not None and "beta" in text and "alpha" not in text,
            text[:60] if text else "nothing arrived",
        )

        # Only beta's daemon should be following. `following` is the scripted
        # engine's record of open `logs?follow=1` connections, so this asks the
        # question at the daemon rather than at the Controller's bookkeeping.
        h.record(
            "and no other host's daemon is following",
            "a tail that fans out holds an open connection on every uplink in "
            "the fleet for a container on one of them",
            container_id in beta.engine.following and not alpha.engine.following,
            f"beta={sorted(beta.engine.following)!r} alpha={sorted(alpha.engine.following)!r}",
        )

        # Written after the subscription is open: the half a one-shot read
        # cannot do, checked here for the *right host* rather than at all.
        beta.engine.logs[container_id].append((True, "beta: written while watching"))
        live = await _await_line(
            stream, lambda e: any("while watching" in line.text for line in e.lines)
        )
        h.record(
            "and lines written while watching arrive from it",
            "a tail that only ever delivers the backfill is a slower one-shot "
            "read wearing the interface of a live one",
            live is not None
            and all("beta" in line.text for line in live.lines),
            "arrived from beta" if live else "never arrived",
        )

    # Leaving the tail releases it on the host that had it, and only there.
    assert alpha.engine is not None and beta.engine is not None
    released = await h.until(lambda: container_id not in beta.engine.following)  # type: ignore[union-attr]
    h.record(
        "and closing it releases the follow on that host",
        "the same cancellation the single-agent suite pins, asked of a fleet: "
        "a leak here costs an open daemon connection on a machine the operator "
        "has navigated away from",
        released and not alpha.engine.following,
        f"beta={sorted(beta.engine.following)!r} alpha={sorted(alpha.engine.following)!r}",
    )

    # The partition rule, for live reads. `PartitionWriter` stops an agent
    # writing another host's entities and `scenario_logs_routing` draws the
    # same line for a one-shot read; nothing had drawn it for a subscription,
    # which is the longest-lived way to be wrong about it.
    beta.engine.recorded.calls.clear()
    async with h.provider(alpha).follow(container_id, 100) as stream:
        ended = await _await_line(stream, lambda e: e.done)

    # Refused, and beta's daemon never consulted about it. The refusal must
    # carry a reason: an empty terminal chunk is indistinguishable from a
    # container that had nothing to say, which is the plausible-looking wrong
    # answer this whole scenario exists to rule out.
    beta_untouched = not any(call.endswith("/logs") for call in beta.engine.recorded.calls)
    h.record(
        "one host's agent cannot follow another's container",
        "an agent that opened a tail for an id it does not own would make "
        "every enrolled host a live window onto every other one",
        ended is not None and bool(ended.reason) and beta_untouched,
        (ended.reason if ended and ended.reason else "no refusal arrived")[:70],
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


async def scenario_signed_upgrade(h: Harness) -> None:
    """A release the Controller distributes and cannot forge (ADR-0017).

    The one property of this feature that no unit test on either side can
    observe, because it is a claim about the *seam*: the Controller sends a
    document it did not sign, over the connection it already holds, and a real
    agent binary decides for itself whether to run it -- against a key compiled
    into that binary, which this process does not have.

    Three things are checked and each fails silently otherwise:

    * a correctly signed, higher release is **staged**, with the trigger file
      root's path unit fires on and nothing else touched;
    * a release for a *lower* version is refused, because the floor is the
      binary on the agent's own disk and downgrade is not an operation;
    * a manifest **this harness signs with a key of its own** is refused --
      which is the compromised-Controller case, and the reason the Controller
      holds no key at all.

    Skipped, with a sentence, when the agent under test was built with no
    signing keys. That is what a checkout produces and it is a legitimate
    build: it advertises no `upgrade` capability, and the Controller refuses to
    start a transfer to it rather than sending two megabytes at a host that was
    always going to refuse them.
    """
    alpha = h.hosts["alpha"]
    provider = h.provider(alpha)
    if provider is None:
        h.record("a signed release is staged", "alpha has no provider", False)
        return

    # A skip is *reported*, not silent. `why` is only printed for a failure,
    # so the sentence goes in `detail` -- a run that quietly dropped the one
    # scenario about replacing binaries on other people's machines would read
    # as a run that covered it.
    if not provider.upgradable:
        h.record(
            "a signed release is staged",
            "an agent built with no keys cannot verify a release, which is a build and not a fault",
            True,
            "SKIPPED: this agent advertises no `upgrade` capability, so it was built with "
            "no signing keys. Rebuild with BYSTACK_SIGNING_KEYS=<hex> and pass --signing-key.",
        )
        return
    if SIGNING_KEY is None:
        h.record(
            "a signed release is staged",
            "the public key is compiled into the binary, so this harness cannot derive it",
            True,
            "SKIPPED: this agent can verify releases, but no --signing-key was given, so "
            "nothing here can produce one it would accept.",
        )
        return

    workdir = h.controller.state_dir.parent
    arch = platform.machine()
    artifact = b"\x7fELF" + os.urandom(4096)

    # A version far above anything this tree will ship, so the check being made
    # is the *floor* rather than an accident of what the agent happens to be.
    good = _publish(workdir / "releases", "99.0.0", arch, artifact, SIGNING_KEY)
    outcome = await provider.push_upgrade(good)

    staged_dir = h.controller.state_dir / "alpha" / "upgrade"
    h.record(
        "a signed release is staged",
        "the agent verified a document the Controller could not have made, and staged it",
        outcome.staged,
        outcome.reason or outcome.state,
    )
    h.record(
        "the trigger file is written, and only after the artifact",
        "a path unit that fired on a half-written file would hand root a truncated binary",
        (staged_dir / "trigger").exists()
        and (staged_dir / "staged").read_bytes() == artifact,
        f"in {staged_dir}",
    )

    # Downgrade. The floor is the binary on the disk and there is no stored
    # number anything can lower.
    old = _publish(workdir / "old", "0.0.1", arch, artifact, SIGNING_KEY)
    refused = await provider.push_upgrade(old)
    h.record(
        "an older release is refused",
        "the version floor is the binary already on the host; downgrade is not an operation",
        refused.state == "refused" and "downgrade" in (refused.reason or ""),
        refused.reason or refused.state,
    )

    # A Controller with its own key. This is what a compromised one looks like
    # from the host's side, and it must look like nothing at all.
    forged_key = ed25519.Ed25519PrivateKey.generate()
    forged = _publish(workdir / "forged", "99.9.9", arch, artifact, forged_key)
    rejected = await provider.push_upgrade(forged)
    h.record(
        "a release signed by the Controller itself is refused",
        "the Controller is a distribution channel and is not trusted with content",
        rejected.state == "refused" and "trusts" in (rejected.reason or ""),
        rejected.reason or rejected.state,
    )


def _publish(
    directory: Path,
    version: str,
    arch: str,
    artifact: bytes,
    key: ed25519.Ed25519PrivateKey,
) -> Release:
    """Write a signed release into a directory and read it back as one.

    Deliberately goes through `ReleaseStore` rather than building a `Release`
    by hand: the indexing rules -- the strict manifest parse, the digest check
    against the file beside it -- are part of what is being exercised, and a
    hand-built object would skip them.
    """
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / f"bystack-agent-{arch}"
    binary.write_bytes(artifact)
    document = (
        f"{releases.MAGIC}\n"
        f"name {releases.ARTIFACT}\n"
        f"version {version}\n"
        f"arch {arch}\n"
        f"sha256 {hashlib.sha256(artifact).hexdigest()}\n"
        f"released_at 1755388800\n"
    ).encode()
    binary.with_name(binary.name + ".manifest").write_bytes(document)
    binary.with_name(binary.name + ".manifest.sig").write_bytes(key.sign(document))

    found = ReleaseStore(directory).find(version, arch)
    assert found is not None, f"the harness wrote a release {directory} would not index"
    return found


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
    ("logs routing", scenario_logs_routing),
    ("live logs routing", scenario_live_logs_routing),
    ("logical fan-out", scenario_logical_fanout),
    ("read-only agent", scenario_read_only_agent),
    ("signed upgrade", scenario_signed_upgrade),
]


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


async def run(binary: Path, port: int) -> int:
    global AGENT_BINARY
    AGENT_BINARY = binary

    workdir = Path(tempfile.mkdtemp(prefix="bystack-fleet-"))
    hosts = fleet()

    controller = await conformance_controller.start(workdir, port)
    harness = Harness(hosts, controller)

    print(f"controller : {controller.url} (mutual TLS)")
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
        await controller.stop()
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

    return report("fleet conformance", harness.checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", type=Path, help="path to the agent binary")
    parser.add_argument("--port", type=int, default=8131)
    parser.add_argument(
        "--signing-key",
        type=Path,
        help="the ed25519 private key this agent build was compiled to trust "
        "(scripts/sign-agent.py keygen). Without it the upgrade scenario skips.",
    )
    args = parser.parse_args(argv)

    if not args.agent.exists():
        print(f"no such agent binary: {args.agent}", file=sys.stderr)
        return 2

    if args.signing_key is not None:
        # Read here rather than inside the run, so a wrong path is an error
        # before three agents are spawned rather than a scenario that skips
        # thirteen scenarios later.
        global SIGNING_KEY
        loaded = serialization.load_pem_private_key(args.signing_key.read_bytes(), password=None)
        if not isinstance(loaded, ed25519.Ed25519PrivateKey):
            print(f"{args.signing_key} is not an ed25519 private key", file=sys.stderr)
            return 2
        SIGNING_KEY = loaded

    return asyncio.run(run(args.agent, args.port))


if __name__ == "__main__":
    raise SystemExit(main())
