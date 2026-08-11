"""systemd and /proc payloads -> canonical graph.

The sibling of `providers/docker/mapper.py`, one layer below it: that file
turns an engine's vocabulary into topology, this one turns a machine's. Both
are pure functions with no I/O and no state, and both exist on the Controller
for the reason ADR-0009 gives — the agent detects *that* something changed, and
deciding what it means requires the whole graph.

Two things here are not in the Docker mapper, and both come from the same
place: a machine has no inventory small enough to draw.

**Identity for a process is the operator's rule, not the pid.** ADR-0002's two
layers, arrived at from the other direction. A pid is the physical layer and it
is worse than a container id: recycled by the kernel, and gone on precisely the
event somebody is watching for. The logical layer is the watch entry — "the
thing whose executable is /usr/local/bin/mydaemon" — which survives the restart
and can therefore hold history across it. So one node per rule, and the pids
are attributes of it.

**A watched thing that is not there is still a node.** `load_state:
not-found`, or a rule matching nothing, is an *observation* and the node says
so. The alternative is a card that quietly disappears, which an operator reads
as "I never added it" rather than as "it is not installed".
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from typing import Any, Final

from bystack.core.graph.model import Edge, EdgeKind, Node
from bystack.core.identity import (
    URN,
    NodeKind,
    container_urn,
    host_urn,
    process_urn,
    unit_urn,
)

#: systemd reports timestamps in microseconds since the epoch. Converted here
#: rather than on the agent, which ships systemd's own units upward unread.
_USEC: Final = 1_000_000

#: A container id inside a cgroup path, in the three spellings that occur:
#: cgroupfs driver (`/docker/<id>`), systemd driver (`docker-<id>.scope`) and
#: podman (`libpod-<id>.scope`). Sixty-four hex characters in all of them.
_CGROUP_CONTAINER: Final = re.compile(
    r"(?:/docker[/-]|/libpod-|/crio-)([0-9a-f]{64})(?:\.scope)?"
)

#: The owning unit inside a cgroup path. `.slice` is deliberately excluded --
#: every process on the machine is inside `system.slice` or `user.slice`, so
#: matching it would claim that all of them belong to one unit.
_CGROUP_UNIT: Final = re.compile(
    r"/([^/\s]+\.(?:service|socket|mount|path|timer|scope))"
)

#: Process states from `/proc/<pid>/stat`, in the vocabulary the graph uses.
#:
#: Collapsed deliberately: `R` and `S` and `D` are all "it is running" to an
#: operator looking at a topology map, and the difference between them is a
#: sampling question that belongs to the tools that sample. What is *not*
#: collapsed is `T` and `Z`, because both mean the process exists and is not
#: doing its job -- which a green card would hide.
_PROCESS_STATE: Final[dict[str, str]] = {
    "R": "running",
    "S": "running",
    "D": "running",
    "I": "running",
    "T": "stopped",
    "t": "stopped",
    "Z": "zombie",
    "X": "dead",
    "x": "dead",
}


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def map_unit(
    source: str,
    engine_id: str,
    payload: dict[str, Any],
    *,
    observed_at: float | None = None,
) -> tuple[Node, Edge]:
    """Map one watched systemd unit to a node and its host edge.

    ``status`` is systemd's ``ActiveState`` with one substitution: a unit whose
    file is missing reports ``not-found`` rather than ``inactive``. Both are
    literally true -- a unit that does not exist is certainly not active -- but
    only one of them is the answer to the question being asked. `inactive` on a
    card reads as "stopped, start it", and the operator would click a button
    that cannot work; `not-found` says the name is wrong or the package is not
    installed, which is the actual problem.
    """
    at = observed_at or _now()
    name = payload["Id"]
    urn = unit_urn(engine_id, name)
    load_state = payload.get("LoadState") or ""
    active_state = payload.get("ActiveState") or "unknown"

    node = Node(
        urn=urn,
        kind=NodeKind.UNIT,
        name=name,
        source=source,
        status=_unit_status(load_state, active_state),
        attrs={
            "description": payload.get("Description") or None,
            "load_state": load_state or None,
            # systemd's type-specific detail underneath the generic state.
            # Kept separate rather than folded in: `active`/`exited` is a
            # oneshot that finished and `active`/`running` is a daemon, and an
            # operator diagnosing one needs to know which they are looking at.
            "sub_state": payload.get("SubState") or None,
            # Whether it comes back after a reboot. The single most
            # consequential fact about a unit that is not visible in its
            # current state -- a green card for a service that is not enabled
            # is true right now and misleading tomorrow morning.
            "unit_file_state": payload.get("UnitFileState") or None,
            "main_pid": payload.get("MainPID") or None,
            # Seconds, not systemd's microseconds. A fixed instant rather than
            # a rendered duration, so the browser can draw "for 3 hours"
            # without either side re-sending anything -- the trap
            # `Container.status_text` documents, avoided by carrying the
            # timestamp instead of the prose.
            "active_since": _seconds(payload.get("ActiveEnterTimestamp")),
            # `None` rather than 0 for a unit that has never restarted, so the
            # inspector renders nothing at all for the overwhelming majority.
            # Same call as `restart_count` on a container, and the same reason:
            # "never restarted" and "not asked" are the same answer.
            "restart_count": payload.get("NRestarts") or None,
            # Why it stopped, which `failed` does not say. `oom-kill` in
            # particular is a diagnosis an operator would otherwise go to the
            # journal for, and it is the difference between "the service is
            # broken" and "the machine is out of memory".
            "result": _result(payload.get("Result")),
            "exit_code": _exit_code(payload, active_state),
            "fragment_path": payload.get("FragmentPath") or None,
        },
        observed_at=at,
    )
    return node, Edge(EdgeKind.HOSTS, host_urn(engine_id), urn, source, observed_at=at)


def _unit_status(load_state: str, active_state: str) -> str:
    if load_state in ("not-found", "masked", "error", "bad-setting"):
        return load_state
    return active_state


def _result(result: str | None) -> str | None:
    """systemd's ``Result``, minus the one value that means nothing happened.

    `success` is the value a unit that has never failed carries, so surfacing
    it would put "result: success" on every card on the map -- a row that is
    true, permanent, and pure noise.
    """
    return result if result and result != "success" else None


def _exit_code(payload: dict[str, Any], active_state: str) -> int | None:
    """``ExecMainStatus``, but only where it is a fact about a failure.

    A running unit's last exit status is whatever it was before it started,
    which is not what a reader of that field will assume it is. Zero is
    likewise dropped: a service that exited cleanly did not "exit with 0" in
    any sense worth a row in an inspector.
    """
    if active_state in ("active", "activating"):
        return None
    code = payload.get("ExecMainStatus") or 0
    return int(code) or None


def _seconds(usec: Any) -> float | None:
    """systemd's microsecond timestamps, in the seconds everything else uses.

    Zero is systemd's "never", which is a different thing from "at the epoch"
    and must not become a card claiming the service has been up since 1970.
    """
    try:
        value = int(usec or 0)
    except (TypeError, ValueError):
        return None
    return value / _USEC if value > 0 else None


def build_unit_slice(
    source: str,
    engine_id: str,
    payloads: Sequence[dict[str, Any]],
    *,
    observed_at: float | None = None,
) -> tuple[list[Node], list[Edge]]:
    """The whole unit slice, from the payloads the agent last reported."""
    at = observed_at or _now()
    nodes: list[Node] = []
    edges: list[Edge] = []
    for payload in payloads:
        node, edge = map_unit(source, engine_id, payload, observed_at=at)
        nodes.append(node)
        edges.append(edge)
    return nodes, edges


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------


def map_process(
    source: str,
    engine_id: str,
    payload: dict[str, Any],
    *,
    present: frozenset[URN] = frozenset(),
    observed_at: float | None = None,
) -> tuple[Node, list[Edge]]:
    """Map one watch rule and whatever currently matches it.

    ``present`` is the set of URNs this partition currently declares. It is
    what decides whether the correlation edges below are emitted at all: a
    process inside a container is a genuine relationship and worth drawing, but
    an edge to a node that is not in the graph is not a relationship, it is a
    dangling reference the UI would have to render an endpoint for. So the
    correlation is always an *attribute*, and it becomes an *edge* only when
    the other end is something the operator can actually see.
    """
    at = observed_at or _now()
    watch_id = payload["WatchId"]
    urn = process_urn(engine_id, watch_id)
    instances: list[dict[str, Any]] = list(payload.get("Instances") or [])
    pattern = payload.get("Pattern") or ""

    first = instances[0] if instances else {}
    cgroup = str(first.get("Cgroup") or "")
    container_id = _container_of(cgroup)
    unit_name = _unit_of(cgroup)

    node = Node(
        urn=urn,
        kind=NodeKind.PROCESS,
        name=payload.get("Label") or _display_name(payload, first),
        source=source,
        status=_process_status(instances),
        attrs={
            "match": payload.get("MatchKind"),
            "pattern": pattern,
            # Every pid that matches, not just the first. A rule that was meant
            # to name one daemon and quietly matches nine is a mistake an
            # operator can only see if the number is in front of them.
            "pids": tuple(int(i["Pid"]) for i in instances if i.get("Pid")),
            "instances": len(instances),
            # What matched before the agent's cap, so a truncated list is never
            # presented as the whole truth.
            "matched": int(payload.get("Total") or len(instances)),
            "cmdline": first.get("Cmdline") or None,
            "uid": first.get("Uid") if instances else None,
            # The oldest match, so a rule covering a parent and its workers
            # reports when the thing itself started rather than when the most
            # recently respawned child did.
            "started_at": min(
                (i["StartedAt"] for i in instances if i.get("StartedAt")), default=None
            ),
            # The correlation, as facts. `cgroup` decides both, and deciding is
            # the Controller's half of the seam (ADR-0009 section 1) -- the
            # agent ships the path and does not know what a container is.
            "container_id": container_id,
            "unit": unit_name,
        },
        observed_at=at,
    )

    edges: list[Edge] = [
        Edge(EdgeKind.HOSTS, host_urn(engine_id), urn, source, observed_at=at)
    ]
    if container_id is not None:
        target = container_urn(engine_id, container_id)
        if target in present:
            edges.append(Edge(EdgeKind.RUNS_IN, urn, target, source, observed_at=at))
    elif unit_name is not None:
        target = unit_urn(engine_id, unit_name)
        if target in present:
            edges.append(Edge(EdgeKind.RUNS_IN, urn, target, source, observed_at=at))

    return node, edges


def _process_status(instances: Sequence[dict[str, Any]]) -> str:
    """One state for a rule that may match several processes.

    Worst-wins, and `absent` when nothing matches at all. A rule matching a
    healthy daemon and one zombie must not read as healthy: the zombie is the
    news, and it is exactly what somebody watching a process for is looking
    for.
    """
    if not instances:
        return "absent"
    states = {_PROCESS_STATE.get(str(i.get("State") or ""), "unknown") for i in instances}
    for worst in ("zombie", "dead", "stopped", "unknown"):
        if worst in states:
            return worst
    return "running"


def _display_name(payload: dict[str, Any], first: dict[str, Any]) -> str:
    """What to call a rule nobody labelled.

    The running process's own name where there is one, because that is what
    the operator would call it; the pattern's last path segment otherwise, so
    a rule matching nothing still reads as the thing it is looking for rather
    than as a blank card.
    """
    comm = str(first.get("Comm") or "").strip()
    if comm:
        return comm
    pattern = str(payload.get("Pattern") or "").strip()
    return pattern.rstrip("/").rsplit("/", 1)[-1] or pattern or "process"


def _container_of(cgroup: str) -> str | None:
    match = _CGROUP_CONTAINER.search(cgroup)
    return match.group(1) if match else None


def _unit_of(cgroup: str) -> str | None:
    """The unit that owns this cgroup, if one does.

    The *last* match rather than the first: a unit's cgroup path nests inside
    its slice, and for a `.scope` inside a `.service` the innermost one is the
    thing actually running the process.
    """
    matches = _CGROUP_UNIT.findall(cgroup)
    return matches[-1] if matches else None


def build_process_slice(
    source: str,
    engine_id: str,
    payloads: Sequence[dict[str, Any]],
    *,
    present: Iterable[URN] = (),
    observed_at: float | None = None,
) -> tuple[list[Node], list[Edge]]:
    """The whole process slice, from the payloads the agent last reported."""
    at = observed_at or _now()
    known = frozenset(present)
    nodes: list[Node] = []
    edges: list[Edge] = []
    for payload in payloads:
        node, node_edges = map_process(
            source, engine_id, payload, present=known, observed_at=at
        )
        nodes.append(node)
        edges.extend(node_edges)
    return nodes, edges
