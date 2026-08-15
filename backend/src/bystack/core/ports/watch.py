"""The watch list port: which host entities an operator has selected.

Every other partition in this graph answers "what is there". This one answers
"what did somebody ask for", and that difference is the whole design.

A Docker engine's inventory *is* the topology: the containers it runs are the
thing being managed, all of them, and the agent Lists all of them. A machine's
inventory is not. It has two thousand systemd units and four hundred
processes, and an operator cares about six of them; drawing the rest would
bury the infrastructure they came to look at, and *observing* the rest would
cost CPU on every managed host forever — which is the second-rate cAdvisor
ARCHITECTURE section 12 exists to refuse.

So the membership of the unit and process slices is a **selection**, and a
selection is user intent: ADR-0001's first durable category, the one that
exists precisely because it cannot be rediscovered. A Controller that lost this
would not repair itself from the fleet within seconds like the graph does; it
would come back having forgotten what the operator asked it to watch, and the
services would simply be gone from the map.

Two consequences that are easy to get wrong:

**A watch is not an observation.** An entry for a unit that is not installed,
or a rule that matches no process, is *kept* and reported as such. A watch that
silently observes nothing is indistinguishable from a watch that is working,
and an operator adding `nginx.service` before installing nginx should see
`not-found` rather than an empty space.

**The bound is per host and it is a bound.** ADR-0006 clause 4 asks every
durable store for a retention policy, and for configuration the honest one is
a ceiling on how much intent may be expressed: this list becomes work on
somebody else's machine, and an unbounded one is an unbounded scan of /proc
authored from a text box.

Nothing in this module may import anything outside the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

#: Watch entries one host may hold.
#:
#: Generous against any real operator — nobody is managing sixty-four services
#: on one machine through a topology map — and finite against the failure this
#: exists for, which is not malice but a stuck client posting the same entry in
#: a loop. Each entry is work the agent performs on every scan, so this number
#: is a promise to the managed host rather than a database limit.
MAX_ENTRIES: Final = 64

#: Hosts one act of selection may reach.
#:
#: The per-host ceiling above is a promise to one machine; this is the promise
#: to the fleet, and it exists because "watch this everywhere" turns a single
#: click into a store write, an fsync and a frame per host. Set well above any
#: fleet a topology map is legible for and well below the number at which one
#: request becomes a way to make the Controller unresponsive to every other.
MAX_FANOUT: Final = 256

#: Longest unit name or match pattern accepted.
#:
#: systemd's own limit is 255 bytes for a unit name, and a pattern longer than
#: a unit name is a paste rather than an intention.
MAX_PATTERN: Final = 255

#: Unit suffixes systemd recognises. A name with none of them gets `.service`
#: appended, which is exactly what `systemctl start nginx` does — refusing it
#: instead would be a pedantry tax paid by whoever is typing at the time.
UNIT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".service", ".socket", ".timer", ".target", ".mount", ".automount",
        ".path", ".slice", ".scope", ".device", ".swap",
    }
)


class WatchKind(StrEnum):
    """What sort of thing is being watched."""

    UNIT = "unit"
    PROCESS = "process"


class MatchKind(StrEnum):
    """How a process watch decides what it is looking at.

    Substring and equality only, and deliberately not a regular expression.
    The pattern is authored on the Controller and evaluated inside a process on
    a machine we do not own, against every entry in /proc — a regex engine
    there is a way to spend a remote host's CPU by typing into a text box, and
    the feature it would buy is one an operator can express by choosing a
    different field (ADR-0016).
    """

    NAME = "name"
    """`/proc/<pid>/comm`, matched exactly. The kernel truncates it to 15
    characters, which is why it is offered alongside the other two rather than
    on its own."""

    EXEC = "exec"
    """The resolved target of `/proc/<pid>/exe`, matched exactly. The most
    precise of the three, and what the inventory picker builds a rule from."""

    CMDLINE = "cmdline"
    """A substring of the full command line. The only one that can tell two
    invocations of the same interpreter apart — `python worker.py` against
    `python api.py` — which is most of what a process watch is for."""


class WatchRejected(ValueError):
    """A watch entry we will not store.

    Raised at the edge of the kernel rather than in the route, so that every
    entry point — the REST API today, an import or a scheduler later — is held
    to the same rules. The messages are written to be shown to whoever typed
    the thing.
    """


@dataclass(frozen=True, slots=True)
class WatchEntry:
    """One thing an operator asked to watch on one host.

    Frozen, like everything else the kernel holds. Editing a watch replaces the
    entry and keeps its :attr:`id`, because an operator fixing a typo in a rule
    is correcting what they meant rather than declaring a different thing — and
    the id is what the node's identity and its history hang from.
    """

    id: str
    """Assigned by the Controller, once, and never derived from the pattern.

    A derived id would change when the pattern was edited, which would silently
    replace the node in the graph and lose everything anchored to it. For a
    process this is also the entity id on the wire, because a process watch has
    no natural name: see `process_urn`.
    """

    engine_id: str
    """Whose host this is. The partition key, and the same engine id the
    agent's certificate binds (ADR-0011)."""

    kind: WatchKind

    group_id: str = ""
    """Which act of selection this entry came from.

    Assigned once, by the Controller, and shared by every entry created in the
    same request: watching `nginx.service` on one host mints a group of one,
    and watching it across nine hosts mints one group with nine members.

    **It is a label on the past, not a rule for the future.** Nothing consults
    it when an entry is stored, sent to a host, or drawn — each entry stays
    exactly as independent as it was before this field existed, and removing
    one member leaves the other eight untouched. What it buys is that a UI can
    say "you also asked for this on eight other machines", which an operator
    otherwise has to reconstruct by reading nine lists.

    That restraint is the whole design. A group that *enforced* membership
    would be a second source of truth about what a host watches, and the
    reconciliation between it and the per-host list is the part that goes
    wrong — a host would acquire entries nobody selected on it, and the
    ADR-0001 category this store belongs to is "user intent", not "policy".
    """

    name: str = ""
    """systemd's own unit name, for :attr:`WatchKind.UNIT`. Empty otherwise."""

    match_kind: MatchKind | None = None
    pattern: str = ""
    """How and what to match, for :attr:`WatchKind.PROCESS`."""

    label: str = ""
    """What the operator calls it, if they said. Never inferred and never
    required: an empty label means the UI shows the unit name or the pattern,
    which is what somebody who did not fill in a display name expects to see."""

    added_at: float = 0.0

    @property
    def target(self) -> str:
        """What the agent is told to look for.

        The unit name or the pattern, whichever this entry is. Used for
        display and for the wire; the *id* is what identity is built from.
        """
        return self.name if self.kind is WatchKind.UNIT else self.pattern


def normalize(entry: WatchEntry) -> WatchEntry:
    """Check one entry and return the form that will be stored.

    Validation and normalization in one pass, because they are the same
    decision: `nginx` is accepted *as* `nginx.service`, and a name that cannot
    become a legal unit name is refused rather than stored in a shape that
    would fail later, on a host, with the reason a round trip away.

    The URN constructors refuse `:` and `/` too, and would raise on the way
    into the graph. Refusing here means the operator finds out while they are
    still looking at the text box.
    """
    if entry.kind is WatchKind.UNIT:
        name = entry.name.strip()
        if not name:
            raise WatchRejected("a unit watch needs a unit name")
        if len(name) > MAX_PATTERN:
            raise WatchRejected(f"unit name is longer than {MAX_PATTERN} characters")
        if "/" in name or ":" in name:
            raise WatchRejected(f"{name!r} is not a unit name: units contain no '/' or ':'")
        if not any(name.endswith(suffix) for suffix in UNIT_SUFFIXES):
            # What `systemctl start nginx` does. Recorded in the stored entry
            # rather than applied on the way to the agent, so the operator sees
            # the name the platform will actually be talking about.
            name = f"{name}.service"
        return WatchEntry(
            id=entry.id,
            engine_id=entry.engine_id,
            kind=WatchKind.UNIT,
            group_id=entry.group_id,
            name=name,
            label=entry.label.strip(),
            added_at=entry.added_at,
        )

    pattern = entry.pattern.strip()
    if not pattern:
        raise WatchRejected("a process watch needs something to match")
    if len(pattern) > MAX_PATTERN:
        raise WatchRejected(f"pattern is longer than {MAX_PATTERN} characters")
    if entry.match_kind is None:
        raise WatchRejected("a process watch needs a match kind")
    if entry.match_kind is MatchKind.EXEC and not pattern.startswith("/"):
        # An `exec` rule is compared against the resolved target of
        # /proc/<pid>/exe, which is always absolute. A relative one would match
        # nothing, forever, quietly.
        raise WatchRejected(f"an exec match needs an absolute path, got {pattern!r}")
    return WatchEntry(
        id=entry.id,
        engine_id=entry.engine_id,
        kind=WatchKind.PROCESS,
        group_id=entry.group_id,
        match_kind=entry.match_kind,
        pattern=pattern,
        label=entry.label.strip(),
        added_at=entry.added_at,
    )


@runtime_checkable
class WatchStore(Protocol):
    """Per-host watch lists, durable by intent.

    Reads are synchronous and writes are coroutines, for the reason
    :class:`~bystack.core.ports.command.AuditLog` gives: any implementation
    worth having beyond the in-memory one has I/O in it, and a synchronous
    write forces every one of them to do that I/O on the event loop — the same
    loop that drives every agent's pump and every browser's delta stream.
    """

    def entries(self, engine_id: str) -> tuple[WatchEntry, ...]:
        """What this host is watching, oldest first. Never ``None``."""
        ...

    def all(self) -> tuple[WatchEntry, ...]:
        """Every entry, across every host."""
        ...

    def get(self, engine_id: str, entry_id: str) -> WatchEntry | None: ...

    async def add(self, entry: WatchEntry) -> WatchEntry:
        """Store one entry, normalized. Returns the stored form.

        Raises :class:`WatchRejected` for an entry that is malformed, that
        duplicates one already held, or that would take the host past
        :data:`MAX_ENTRIES`.
        """
        ...

    async def remove(self, engine_id: str, entry_id: str) -> bool:
        """Forget one entry. ``False`` if it was not there.

        Not a destructive operation in the ADR-0012 sense and not subject to
        that rule: it deletes a *preference*, on the Controller, and nothing
        about the host changes. The service goes on running exactly as it was;
        it stops being drawn.
        """
        ...
