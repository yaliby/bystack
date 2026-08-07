"""Docker lifecycle operations.

The provider's implementation of
:class:`~bystack.core.ports.command.CommandExecutor`. Two responsibilities,
both of which have to live here rather than a layer up:

**The capability policy.** Which operations make sense for a container
depends on Docker's own state vocabulary -- ``running``, ``paused``,
``exited`` -- and the kernel deliberately does not know that vocabulary. A
provider is the only thing entitled to answer "what can be done to this",
which is also what lets a future Kubernetes or systemd provider answer
differently without touching anything shared.

**The translation.** URN to container id, ``CommandKind`` to Engine endpoint,
Engine status code to :class:`~bystack.core.ports.command.CommandStatus`.

Everything about *whether* an operation is allowed -- read-only mode, who is
asking, what it fans out to -- is decided above this file and never here. A
provider that could authorize its own commands would be a second policy
engine, and the two would disagree within a release.
"""

from __future__ import annotations

import time
from typing import Final

from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind
from bystack.core.ports.command import (
    CommandKind,
    CommandRequest,
    CommandStatus,
    TargetOutcome,
)
from bystack.providers.docker.client import ActionResult, EngineClient

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


async def execute(client: EngineClient, request: CommandRequest, target: Node) -> TargetOutcome:
    """Run one command against one container.

    Returns an outcome for every operational failure. The only way this
    raises is a genuine defect, and the caller treats it as one.
    """
    container_id = target.urn.segments[-1]
    started = time.perf_counter()

    match request.kind:
        case CommandKind.START:
            outcome = await client.start_container(container_id)
        case CommandKind.STOP:
            outcome = await client.stop_container(container_id, grace=request.timeout)
        case CommandKind.RESTART:
            outcome = await client.restart_container(container_id, grace=request.timeout)
        case CommandKind.PAUSE:
            outcome = await client.pause_container(container_id)
        case CommandKind.UNPAUSE:
            outcome = await client.unpause_container(container_id)
        case CommandKind.KILL:
            outcome = await client.kill_container(container_id, signal=request.signal)

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    match outcome.result:
        case ActionResult.APPLIED:
            status, detail = CommandStatus.SUCCEEDED, None
        case ActionResult.UNCHANGED:
            status, detail = CommandStatus.NOOP, "already in the requested state"
        case ActionResult.NOT_FOUND:
            # The container was recreated between the operator seeing it and
            # the request landing -- the ordinary outcome of clicking restart
            # on something a deploy is already replacing. Reported plainly
            # rather than dressed up as a server error.
            status, detail = CommandStatus.FAILED, outcome.detail or "container no longer exists"
        case ActionResult.CONFLICT:
            status, detail = CommandStatus.FAILED, outcome.detail or "engine refused the transition"
        case ActionResult.ERROR:
            status, detail = CommandStatus.FAILED, outcome.detail or "engine error"

    return TargetOutcome(
        target=target.urn, status=status, detail=detail, duration_ms=elapsed_ms
    )
