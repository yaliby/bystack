"""The parity gate's comparison.

`parity.py` is the harness `docs/MIGRATION.md` §3 hangs the deletion of the
agentless path on, and its whole value is the ability to *fail*. A comparison
that silently always agrees would close the gate on nothing and take the
oracle down with it, so every divergence it claims to catch is asserted here
against hand-built partitions -- including the one where both graphs are
empty and agree perfectly.
"""

from __future__ import annotations

from bystack.conformance.parity import Check, Partition, compare
from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.identity import NodeKind, container_urn, host_urn, network_urn

HOST = host_urn("e1")
C1 = container_urn("e1", "c1")
C2 = container_urn("e1", "c2")
NET = network_urn("e1", "net1")


class Recorder:
    """The slice of `Harness` that `compare` actually touches."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def record(self, name: str, why: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, why, passed, detail))
        return passed

    def result(self, name: str) -> Check:
        return next(c for c in self.checks if c.name == name)

    def failures(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


def container(urn, name="shop-web-1", status="running", **attrs) -> Node:
    return Node(
        urn=urn, kind=NodeKind.CONTAINER, name=name, source="e1", status=status, attrs=attrs
    )


def full(**overrides) -> Partition:
    """A partition with one of every kind the vacuity check requires."""
    nodes = [
        Node(urn=HOST, kind=NodeKind.HOST, name="host", source="e1"),
        Node(urn=network_urn("e1", "img"), kind=NodeKind.IMAGE, name="nginx", source="e1"),
        container(C1),
    ]
    edges = [Edge(kind=EdgeKind.HOSTS, src=HOST, dst=C1, source="e1")]
    partition = Partition(
        label="p", nodes={n.urn: n for n in nodes}, edges={e.key: e for e in edges}
    )
    for field, value in overrides.items():
        setattr(partition, field, value)
    return partition


def test_identical_partitions_agree() -> None:
    recorder = Recorder()
    compare(recorder, full(), full())
    assert recorder.failures() == []


def test_two_empty_graphs_do_not_pass() -> None:
    # The failure mode that matters most: an unreachable or idle socket makes
    # every other check agree about nothing.
    empty = Partition(label="p")
    recorder = Recorder()
    compare(recorder, empty, empty)

    assert "the comparison is not vacuous" in recorder.failures()


def test_entity_the_agent_missed_is_caught() -> None:
    oracle = full()
    oracle.nodes[C2] = container(C2, name="shop-db-1")

    recorder = Recorder()
    compare(recorder, oracle, full())

    assert "same entities" in recorder.failures()
    assert C2 in recorder.result("same entities").detail


def test_entity_the_agent_invented_is_caught() -> None:
    subject = full()
    subject.nodes[C2] = container(C2, name="shop-db-1")

    recorder = Recorder()
    compare(recorder, full(), subject)

    assert "same entities" in recorder.failures()
    assert "invented" in recorder.result("same entities").detail


def test_dropped_attribute_is_caught_and_named() -> None:
    # The defect the gate exists for: the agent deserializes only the fields
    # it declares, so one it does not reaches the mapper as absent and
    # produces a node that is well-formed and quietly incomplete.
    oracle = full()
    oracle.nodes[C1] = container(C1, image="nginx:latest", created=1699)
    subject = full()
    subject.nodes[C1] = container(C1, image="nginx:latest")

    recorder = Recorder()
    compare(recorder, oracle, subject)

    assert "same content" in recorder.failures()
    diff = recorder.result(f"  content of {C1}").detail
    assert "attrs[created]" in diff
    assert "agent=None" in diff


def test_status_drift_is_caught() -> None:
    oracle = full()
    subject = full()
    subject.nodes[C1] = container(C1, status="exited")

    recorder = Recorder()
    compare(recorder, oracle, subject)

    assert "same content" in recorder.failures()
    assert "status" in recorder.result(f"  content of {C1}").detail


def test_observed_at_alone_is_not_drift() -> None:
    # The two paths discover the same daemon at different moments. If that
    # counted as divergence the gate could never close.
    oracle = full()
    subject = full()
    subject.nodes[C1] = Node(
        urn=C1, kind=NodeKind.CONTAINER, name="shop-web-1", source="e1",
        status="running", observed_at=999.0,
    )

    recorder = Recorder()
    compare(recorder, oracle, subject)

    assert recorder.failures() == []


def test_missing_edge_is_caught() -> None:
    subject = full()
    subject.edges = {}

    recorder = Recorder()
    compare(recorder, full(), subject)

    assert "same topology" in recorder.failures()


def test_edge_attribute_drift_is_caught() -> None:
    # A published port lives on the edge; matching endpoints say nothing
    # about it.
    oracle = full()
    oracle.edges = {
        e.key: e
        for e in [Edge(kind=EdgeKind.HOSTS, src=HOST, dst=C1, source="e1", attrs={"port": 8080})]
    }
    subject = full()
    subject.edges = {
        e.key: e for e in [Edge(kind=EdgeKind.HOSTS, src=HOST, dst=C1, source="e1")]
    }

    recorder = Recorder()
    compare(recorder, oracle, subject)

    assert "same topology" not in recorder.failures()
    assert "same edge attributes" in recorder.failures()
