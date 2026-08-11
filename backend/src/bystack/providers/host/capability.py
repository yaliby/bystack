"""What may be done to a unit or a process in each state.

The sibling of `providers/docker/capability.py`, and it exists for the reason
that file names: only something that speaks a vocabulary is entitled to say
what can be done to something described in it. The kernel does not know
systemd's states any more than it knows Docker's, and this is where a third
provider would answer differently without touching anything shared.

**No new verb was needed, and that is the load-bearing part.** `CommandKind` is
closed at six reversible lifecycle transitions, permanently, because ADR-0014
decided this platform will never know who is asking. Everything below maps into
that existing set: `start`/`stop`/`restart` are systemd's own words, `kill` is
`KillUnit` and `SIGKILL`, and `pause`/`unpause` are `SIGSTOP`/`SIGCONT`. Nothing
here enables, disables, masks or writes a unit file — each of those changes what
the machine does *after the next reboot*, which is not reversible by watching
the graph go back to how it was, and none of them is a lifecycle transition.
Adding one is a change to ADR-0014's premise, not to a dict.
"""

from __future__ import annotations

from typing import Final

from bystack.core.graph.model import Node
from bystack.core.identity import NodeKind
from bystack.core.ports.command import CommandKind

#: What each systemd `ActiveState` permits, plus the `LoadState` values the
#: mapper substitutes for it.
#:
#: `restart` is offered on `failed` and not on `inactive`, which looks
#: inconsistent and is not: systemd's `RestartUnit` starts a stopped unit
#: perfectly well, but an operator looking at a unit that is simply *off* wants
#: `start`, and two buttons that do the same thing are two chances to wonder
#: which one is correct. On a failed unit, `restart` is the one people reach
#: for, so both are there.
_UNIT_BY_STATE: Final[dict[str, frozenset[CommandKind]]] = {
    "active": frozenset({CommandKind.STOP, CommandKind.RESTART, CommandKind.KILL}),
    "inactive": frozenset({CommandKind.START}),
    "failed": frozenset({CommandKind.START, CommandKind.RESTART}),
    # In flux, both of them. `kill` is what breaks a unit stuck in
    # `activating` behind a `ExecStartPre` that will never return, and it is
    # the only thing that works on one -- `stop` queues a job behind the one
    # already running.
    "activating": frozenset({CommandKind.STOP, CommandKind.KILL}),
    "deactivating": frozenset({CommandKind.KILL}),
    # The unit file is not there, or is masked. Nothing can be done through
    # this platform, and offering `start` on a unit systemd will refuse to
    # load is a button whose only possible outcome is an error toast.
    "not-found": frozenset(),
    "masked": frozenset(),
    "error": frozenset(),
    "bad-setting": frozenset(),
}

#: What each process state permits.
#:
#: **`start` and `restart` appear nowhere, and that is a design decision rather
#: than a gap.** A bare process is not a unit: there is no recorded way to
#: launch it, and the only way to offer `start` would be for the Controller to
#: hold a command line and have the agent execute it — which, with no user
#: identity in front of the API (ADR-0014), is arbitrary code execution as root
#: on every managed host for anyone who can reach the port. A process watch can
#: stop something and can watch it; starting it belongs to whatever supervises
#: it, and if the answer is "nothing does", the honest fix is a unit file.
_PROCESS_BY_STATE: Final[dict[str, frozenset[CommandKind]]] = {
    "running": frozenset({CommandKind.STOP, CommandKind.KILL, CommandKind.PAUSE}),
    # SIGSTOP'd. `stop` and `kill` still work on a stopped process -- SIGTERM
    # is queued and delivered on SIGCONT, SIGKILL takes it immediately -- and
    # `unpause` is the one that undoes what somebody did here.
    "stopped": frozenset({CommandKind.UNPAUSE, CommandKind.STOP, CommandKind.KILL}),
    # A zombie is already dead; it is waiting for a parent that has not reaped
    # it. Signals do nothing to one, by definition, so every button would be a
    # button that silently fails. The fix is to signal the *parent*, which is a
    # different target and one the operator can watch separately.
    "zombie": frozenset(),
    "dead": frozenset(),
    # Nothing matches the rule. Not an error -- it is the answer for a daemon
    # that is not currently running -- and there is nothing to signal.
    "absent": frozenset(),
}

_NOTHING: Final[frozenset[CommandKind]] = frozenset()


def supported_commands(node: Node) -> frozenset[CommandKind]:
    """Which commands are meaningful for this host entity right now.

    Anything that is not a unit or a process gets nothing, exactly as the
    Docker table returns nothing for anything that is not a container. A state
    neither table has seen is likewise nothing: a newer systemd inventing a
    state must degrade to a read-only view of that unit, never to a traceback
    in the API.
    """
    match node.kind:
        case NodeKind.UNIT:
            return _UNIT_BY_STATE.get(node.status or "", _NOTHING)
        case NodeKind.PROCESS:
            return _PROCESS_BY_STATE.get(node.status or "", _NOTHING)
        case _:
            return _NOTHING
