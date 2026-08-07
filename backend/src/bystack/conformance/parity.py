"""Parity conformance: does the agent build the graph the oracle builds?

    python -m bystack.conformance.parity ../agent/target/release/bystack-agent

`runner.py` proves the agent behaves against a *scripted* daemon, and
`fleet.py` proves the architecture holds across several of them. Both script
the engine, which means both are blind to the same thing: what a **real**
daemon actually puts on the wire.

That gap is what `docs/MIGRATION.md` §3 calls the correctness gate, and §6
hangs the deletion of the whole agentless path on it. The agentless
implementation is the oracle -- it has been reading real daemons since before
the pivot -- so the gate is not "does the agent look right", it is "do the two
implementations, pointed at one daemon, produce the same graph".

**Why the two graphs are comparable at all.** `ingest.py` rebuilds Docker's
payload shapes and hands them to the same unmodified `mapper.py` the informer
uses, so identity and topology have one implementation on both paths. What
differs is everything upstream of the mapper: which fields the agent's Rust
structs deserialize, what its hashing considers a change, and what its slices
claim authority over. A field the agent silently drops reaches the mapper as
absent rather than as an error -- it produces a node that is well-formed,
plausible, and quietly missing an attribute. Nothing at runtime would say so.
That is the class of defect this exists to catch, and comparing content
hashes catches all of it at once.

Two Controllers, two stores, one daemon. Separate stores are not incidental:
both paths derive URNs from the same engine id, so they produce *the same
URNs*, and a single store would have them overwrite each other's nodes -- the
comparison would be against whichever wrote last.

This run mutates nothing. It needs read access to the socket and an agent
binary, and it is safe against a daemon carrying real workloads.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn

from bystack.api.app import API_PREFIX, build_context, create_app
from bystack.api.deps import AppContext
from bystack.config import AgentsConfig, HostConfig, Settings
from bystack.core.graph.model import Edge, EdgeKey, Node
from bystack.core.identity import URN, engine_scope
from bystack.core.ports.provider import ProviderState
from bystack.core.ports.transport import TransportError
from bystack.infra.transports.local_socket import LocalSocketConfig, LocalSocketTransport
from bystack.providers.docker.client import EngineClient

#: How long each path gets to reach READY and stop moving.
#:
#: Generous, and deliberately not a timeout on an operation: a real daemon
#: with a few hundred containers takes longer to enumerate than a scripted one
#: with four, and a slow machine must fail as "slow", never as "divergent".
SETTLE = 30.0

#: A graph is considered settled once its sequence number holds still this
#: long. Both paths reconcile on a timer, so "stopped changing" is the only
#: available definition of "done".
QUIET = 2.0

#: Kinds the oracle must actually have found for a comparison to mean
#: anything. Two empty graphs agree perfectly.
EXPECTED_KINDS = ("host", "container", "image")


@dataclass
class Check:
    name: str
    why: str
    passed: bool
    detail: str = ""


@dataclass
class Partition:
    """One path's view of the daemon."""

    label: str
    nodes: dict[URN, Node] = field(default_factory=dict)
    edges: dict[EdgeKey, Edge] = field(default_factory=dict)

    @classmethod
    def of(cls, label: str, context: AppContext) -> Partition:
        snapshot = context.store.snapshot()
        return cls(
            label=label,
            nodes={n.urn: n for n in snapshot.nodes},
            edges={e.key: e for e in snapshot.edges},
        )

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.kind] = counts.get(node.kind, 0) + 1
        return counts


def _by_kind(urns: Iterable[URN]) -> str:
    """Summarize a URN set as `3 container, 1 network`."""
    counts: dict[str, int] = {}
    for urn in urns:
        counts[urn.kind] = counts.get(urn.kind, 0) + 1
    return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) or "none"


def _sample(items: Iterable[str], limit: int = 6) -> str:
    ordered = sorted(items)
    shown = ", ".join(ordered[:limit])
    return f"{shown}, +{len(ordered) - limit} more" if len(ordered) > limit else shown


class Harness:
    def __init__(self, oracle: AppContext, subject: AppContext) -> None:
        self.oracle = oracle
        self.subject = subject
        self.checks: list[Check] = []

    def record(self, name: str, why: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, why, passed, detail))
        return passed

    def snapshot(self) -> tuple[Partition, Partition]:
        return (
            Partition.of("agentless", self.oracle),
            Partition.of("agent", self.subject),
        )

    # -- settling ---------------------------------------------------------

    def states(self) -> tuple[ProviderState | None, ProviderState | None]:
        def state(context: AppContext) -> ProviderState | None:
            health = context.collector.health()
            return next(iter(health.values())).state if health else None

        return state(self.oracle), state(self.subject)

    async def settle(self, within: float = SETTLE) -> bool:
        """Wait for both paths to go READY and then hold still.

        Sequence numbers rather than node counts: a partition that has all its
        nodes but is still rewriting their content is not done, and node
        counts cannot see the difference.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        marks: tuple[int, int] | None = None
        still_since = 0.0

        while loop.time() < deadline:
            if all(state is ProviderState.READY for state in self.states()):
                current = (self.oracle.store.seq, self.subject.store.seq)
                if current != marks:
                    marks, still_since = current, loop.time()
                elif loop.time() - still_since >= QUIET:
                    return True
            await asyncio.sleep(0.1)
        return False


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


def compare(harness: Harness, oracle: Partition, subject: Partition) -> None:
    # A vacuous pass is the failure mode that matters most here: if the socket
    # were empty, or both paths were broken in the same direction, every check
    # below would agree perfectly about nothing.
    counts = oracle.kinds()
    missing_kinds = [kind for kind in EXPECTED_KINDS if not counts.get(kind)]
    harness.record(
        "the comparison is not vacuous",
        "two empty graphs agree perfectly; a gate that can pass on an idle "
        "socket is not a gate",
        not missing_kinds,
        f"oracle found {_by_kind(oracle.nodes)}"
        + (f"; nothing of kind: {', '.join(missing_kinds)}" if missing_kinds else ""),
    )

    only_oracle = oracle.nodes.keys() - subject.nodes.keys()
    only_subject = subject.nodes.keys() - oracle.nodes.keys()
    shared = oracle.nodes.keys() & subject.nodes.keys()

    detail = f"{len(shared)} shared ({_by_kind(shared)})"
    if only_oracle:
        detail += f"\n         agent missed: {_sample(only_oracle)}"
    if only_subject:
        detail += f"\n         agent invented: {_sample(only_subject)}"
    harness.record(
        "same entities",
        "an entity one path sees and the other does not is a hole in the "
        "topology the UI draws, and it is invisible until someone compares",
        not (only_oracle or only_subject),
        detail,
    )

    # Content, not identity. `revision` is the mapper's own content hash --
    # urn, kind, name, status, labels, attrs -- which is exactly the set of
    # fields a dropped field on the agent's side would change, and it excludes
    # `observed_at`, which legitimately differs between two paths that
    # discovered the same thing at different moments.
    drifted = sorted(
        urn for urn in shared if oracle.nodes[urn].revision != subject.nodes[urn].revision
    )
    harness.record(
        "same content",
        "the agent declares only the Docker fields it deserializes; one it "
        "does not reaches the mapper as absent rather than as an error, and "
        "produces a node that is well-formed and quietly wrong",
        not drifted,
        f"{len(shared)} entities compared by content hash"
        + ("" if not drifted else f"\n         drifted: {_sample(drifted)}"),
    )

    for urn in drifted[:3]:
        harness.record(
            f"  content of {urn}",
            "the field-level diff behind the hash mismatch above",
            False,
            _field_diff(oracle.nodes[urn], subject.nodes[urn]),
        )

    only_oracle_edges = oracle.edges.keys() - subject.edges.keys()
    only_subject_edges = subject.edges.keys() - oracle.edges.keys()
    shared_edges = oracle.edges.keys() & subject.edges.keys()

    edge_detail = f"{len(shared_edges)} shared"
    if only_oracle_edges:
        edge_detail += f"\n         agent missed: {_sample(only_oracle_edges)}"
    if only_subject_edges:
        edge_detail += f"\n         agent invented: {_sample(only_subject_edges)}"
    harness.record(
        "same topology",
        "edges are the product, not a detail: a missing `attached_to` is a "
        "container that appears to be on no network at all",
        not (only_oracle_edges or only_subject_edges),
        edge_detail,
    )

    edge_drift = sorted(
        key
        for key in shared_edges
        if dict(oracle.edges[key].attrs) != dict(subject.edges[key].attrs)
    )
    harness.record(
        "same edge attributes",
        "a published port or an ip address lives on the edge; the endpoints "
        "matching says nothing about it",
        not edge_drift,
        f"{len(shared_edges)} edges compared"
        + ("" if not edge_drift else f"\n         drifted: {_sample(edge_drift)}"),
    )


def _field_diff(oracle: Node, subject: Node) -> str:
    """Which field of a node the two paths disagree about."""
    lines: list[str] = []
    for name in ("kind", "name", "status"):
        left, right = getattr(oracle, name), getattr(subject, name)
        if left != right:
            lines.append(f"{name}: agentless={left!r} agent={right!r}")

    for name in ("labels", "attrs"):
        left_map = dict(getattr(oracle, name))
        right_map = dict(getattr(subject, name))
        for key in sorted(left_map.keys() | right_map.keys()):
            if left_map.get(key) != right_map.get(key):
                lines.append(
                    f"{name}[{key}]: agentless={left_map.get(key)!r} "
                    f"agent={right_map.get(key)!r}"
                )
    return "\n         ".join(lines) or "content hash differs but no field does"


async def hold(harness: Harness, seconds: float) -> None:
    """Re-compare for a while, so a daemon that is changing is caught changing.

    A single comparison proves the two paths converge on a quiet daemon. It
    says nothing about whether they converge on the *same* thing after a real
    event -- and events on a real daemon are the part no scripted engine has
    ever exercised. Anything the operator does to the daemon during this
    window is checked for free.
    """
    if seconds <= 0:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    divergences = 0
    observed = 0
    start = (harness.oracle.store.seq, harness.subject.store.seq)

    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        oracle, subject = harness.snapshot()
        observed += 1
        if oracle.nodes.keys() != subject.nodes.keys() or any(
            oracle.nodes[urn].revision != subject.nodes[urn].revision
            for urn in oracle.nodes.keys() & subject.nodes.keys()
        ):
            divergences += 1

    moved = (harness.oracle.store.seq, harness.subject.store.seq) != start

    # Transient disagreement is expected and is not a defect: the two paths
    # observe an event independently and neither is obliged to see it first.
    # A *settled* disagreement is. So the verdict is one final comparison after
    # letting both paths come to rest, and the sample count is reported beside
    # it as context rather than judged.
    settled = await harness.settle()
    oracle, subject = harness.snapshot()
    diverged = sorted(oracle.nodes.keys() ^ subject.nodes.keys()) + sorted(
        urn
        for urn in oracle.nodes.keys() & subject.nodes.keys()
        if oracle.nodes[urn].revision != subject.nodes[urn].revision
    )

    harness.record(
        "agreement holds while the daemon runs",
        "convergence on a quiet daemon is the easy half; the two paths must "
        "also land on the same graph after a real event",
        settled and not diverged,
        f"{observed} samples over {seconds:.0f}s, {divergences} transient, "
        + ("daemon changed during the window" if moved else "daemon was quiet")
        + ("" if settled else "\n         never came to rest after the window")
        + ("" if not diverged else f"\n         still disagreeing: {_sample(diverged)}"),
    )


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


async def probe(socket: Path) -> str:
    """Read the engine id, and fail early and precisely if we cannot.

    Uses the platform's own transport so that the "you are not in the docker
    group" message is the one the product already knows how to give, rather
    than a second copy of it that could drift.
    """
    transport = LocalSocketTransport("parity-probe", LocalSocketConfig(path=str(socket)))
    endpoint = await transport.open()
    client = EngineClient(endpoint)
    try:
        info = await client.info()
    finally:
        await client.aclose()
        await transport.close()

    engine_id = str(info.get("ID") or "")
    if not engine_id:
        raise TransportError(f"{socket} answered /info without an ID")
    return engine_id


async def run(agent_binary: Path, socket: Path, port: int, watch: float) -> int:
    engine_id = await probe(socket)
    source = engine_scope(engine_id)

    # The oracle's partition key is aligned to the engine scope the agent will
    # adopt. Both paths already produce identical URNs -- they derive them from
    # the same engine id -- and aligning `source` too makes the two partitions
    # differ in nothing at all when they agree, so any diff below is real.
    oracle = build_context(
        Settings(
            hosts=[
                HostConfig(
                    id=source,
                    transport=LocalSocketConfig(path=str(socket)),
                    resync_interval=10.0,
                )
            ],
            read_only=True,
        )
    )

    app = create_app(
        Settings(
            hosts=[],
            read_only=True,
            agents=AgentsConfig(enabled=True, auto_approve=True, resync_interval=60),
        )
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.05)

    url = f"ws://127.0.0.1:{port}{API_PREFIX}/agents/connect"
    print(f"daemon socket : {socket}")
    print(f"engine        : {engine_id} -> partition {source}")
    print(f"controller    : {url}")
    print(f"agent         : {agent_binary}\n")

    await oracle.collector.start()
    agent = await asyncio.create_subprocess_exec(
        str(agent_binary),
        "--controller", url,
        "--socket", str(socket),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "RUST_BACKTRACE": "1"},
    )

    harness = Harness(oracle, app.state.context)
    try:
        settled = await harness.settle()
        oracle_state, subject_state = harness.states()
        if not harness.record(
            "both paths reached the daemon",
            "everything below compares two graphs; if either path never "
            "became authoritative there is nothing to compare",
            settled,
            f"agentless={oracle_state}, agent={subject_state}",
        ):
            return report(harness)

        compare(harness, *harness.snapshot())
        await hold(harness, watch)
    finally:
        with contextlib.suppress(ProcessLookupError):
            agent.terminate()
        if agent.stdout is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                output = await asyncio.wait_for(agent.stdout.read(), timeout=2.0)
                if output:
                    print("--- agent output ---")
                    print(output.decode().rstrip())
                    print()
        await oracle.collector.stop()
        await oracle.bus.aclose()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serving, timeout=5.0)

    return report(harness)


def report(harness: Harness) -> int:
    print("--- parity ---")
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
    if not failed:
        print(
            "\nThe agent reproduces the oracle's graph on this daemon.\n"
            "This is the gate in docs/MIGRATION.md section 3; step 6 -- deleting the\n"
            "transports, the client and the informer -- is unblocked by it."
        )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", type=Path, help="path to the agent binary")
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path("/var/run/docker.sock"),
        help="the daemon both paths read (default: the local Docker socket)",
    )
    parser.add_argument("--port", type=int, default=8132)
    parser.add_argument(
        "--watch",
        type=float,
        default=10.0,
        help="seconds to keep comparing after settling; churn the daemon during it",
    )
    args = parser.parse_args(argv)

    if not args.agent.exists():
        print(f"no such agent binary: {args.agent}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(args.agent, args.socket, args.port, args.watch))
    except TransportError as exc:
        print(f"cannot read the daemon: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
