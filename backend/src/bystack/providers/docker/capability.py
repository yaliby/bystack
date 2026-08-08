"""Docker's capability policy: what may be done to a container in each state.

Which operations make sense for a container depends on Docker's own state
vocabulary -- ``running``, ``paused``, ``exited`` -- and the kernel
deliberately does not know that vocabulary. Only something that speaks Docker
is entitled to answer "what can be done to this", which is what lets a future
Kubernetes or systemd provider answer differently without touching anything
shared.

Everything about *whether* an operation is allowed -- read-only mode, who is
asking, what it fans out to -- is decided above this file and never here. A
module that could authorize its own commands would be a second policy engine,
and the two would disagree within a release.

**This outlived the provider it was written for.** It used to sit beside the
translation half -- URN to container id, ``CommandKind`` to Engine endpoint --
which went with the agentless path (`docs/MIGRATION.md` §6). The policy did
not, because it never touched a socket: it is a pure function of a node that
is already in the graph, and the graph is filled by agents now.

:class:`AgentProvider` consults it, which it did not always do -- for a while
``GET /commands/actions`` was state-blind for every host, on the grounds that
a cached view is a staler opinion than the check the agent applies anyway.
What settled it is that the node passed in here is the same node the dashboard
is drawing: agreeing with it is coherence, not a second opinion, and a card
reading `running` above a `Start` button was wrong on its own terms before
freshness entered into it.
"""

from __future__ import annotations

from typing import Final

from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind
from bystack.core.ports.command import CommandKind

#: What each container state permits.
#:
#: Keyed by Docker's ``State`` field from ``/containers/json``, which is a
#: closed vocabulary -- unlike ``Status``, the rendered string next to it that
#: reads "Up 3 hours" and must never be depended on for anything.
#:
#: The empty entries are as deliberate as the populated ones. A ``dead``
#: container cannot be started; it can only be removed, and this platform does
#: not remove things yet. Offering a button that is guaranteed to fail is
#: worse than offering none.
_BY_STATE: Final[dict[str, frozenset[CommandKind]]] = {
    "running": frozenset(
        {CommandKind.STOP, CommandKind.RESTART, CommandKind.PAUSE, CommandKind.KILL}
    ),
    # `stop` on a paused container is correct and the engine handles the
    # unpause itself -- it is how you stop something you paused without first
    # resuming the workload you paused to quiesce.
    "paused": frozenset({CommandKind.UNPAUSE, CommandKind.STOP}),
    "created": frozenset({CommandKind.START}),
    "exited": frozenset({CommandKind.START, CommandKind.RESTART}),
    # In flux. Anything that races the engine's own restart loop is a bad
    # idea, but stopping it is exactly how an operator breaks a crash loop.
    "restarting": frozenset({CommandKind.STOP, CommandKind.KILL}),
    "removing": frozenset(),
    "dead": frozenset(),
}

#: States a container may report that we have never seen. Treated as "no
#: operations available" rather than as an error: a newer daemon inventing a
#: state must degrade to a read-only view of that container, never to a
#: traceback in the API.
_UNKNOWN_STATE: Final[frozenset[CommandKind]] = frozenset()


def supported_commands(node: Node) -> frozenset[CommandKind]:
    """Which commands are meaningful for this node right now.

    Non-containers get nothing. Logical nodes (a service, a stack) are
    resolved to their containers before they ever reach a provider, so a
    service URN arriving here would be a bug upstream, not a case to handle.
    """
    if node.kind != NodeKind.CONTAINER:
        return _UNKNOWN_STATE
    return _BY_STATE.get(node.status or "", _UNKNOWN_STATE)
