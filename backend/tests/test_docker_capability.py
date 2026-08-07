"""Docker's capability policy: what each container state permits.

The engine-status half of this file went with the agentless executor
(`docs/MIGRATION.md` §6). This half never touched a socket -- it is a pure
function of a node already in the graph -- and it is the encoding of Docker's
state vocabulary, which is the expensive thing to re-derive correctly later.

Nothing consults it at present; see `providers/docker/capability.py`.
"""

from __future__ import annotations

import pytest

from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind, container_urn, network_urn, service_urn
from bystack.core.ports.command import CommandKind
from bystack.providers.docker.capability import supported_commands

ENGINE = "e1"
CID = "c" * 64
URN = container_urn(ENGINE, CID)


def container(status: str) -> Node:
    return Node(
        urn=URN, kind=NodeKind.CONTAINER, name="web", source="docker-a", status=status
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", {CommandKind.STOP, CommandKind.RESTART, CommandKind.PAUSE,
                     CommandKind.KILL}),
        ("paused", {CommandKind.UNPAUSE, CommandKind.STOP}),
        ("exited", {CommandKind.START, CommandKind.RESTART}),
        ("created", {CommandKind.START}),
        ("restarting", {CommandKind.STOP, CommandKind.KILL}),
    ],
)
def test_each_container_state_permits_what_it_can_actually_do(state, expected) -> None:
    assert set(supported_commands(container(state))) == expected


@pytest.mark.parametrize("state", ["dead", "removing"])
def test_terminal_states_offer_nothing(state) -> None:
    """A dead container can only be removed, and this platform does not remove
    things yet. Offering a button guaranteed to fail is worse than none."""
    assert supported_commands(container(state)) == frozenset()


def test_an_unrecognized_state_degrades_to_read_only() -> None:
    """A newer daemon inventing a state must produce a read-only view of that
    container, never a traceback in the API."""
    assert supported_commands(container("hibernating")) == frozenset()


@pytest.mark.parametrize(
    "node",
    [
        Node(urn=network_urn(ENGINE, "n1"), kind=NodeKind.NETWORK, name="n", source="s"),
        Node(urn=service_urn(ENGINE, "p", "web"), kind=NodeKind.SERVICE, name="web",
             source="s"),
    ],
)
def test_non_containers_have_no_operations_here(node) -> None:
    """Logical nodes are expanded to containers before reaching a provider, so
    one arriving here is an upstream bug, not a case to serve."""
    assert supported_commands(node) == frozenset()
