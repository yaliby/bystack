"""The audit log, in both forms.

`memory.py` is bounded and forgets; `durable.py` is bounded and does not. The
cases that matter are the ones a restart is involved in, because that is the
entire difference between them and it is the difference ADR-0012 puts in front
of every destructive verb.

Nothing here touches Docker or the network. The one thing it touches is
`tmp_path`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bystack.api.app import API_PREFIX, create_app
from bystack.config import AgentsConfig, AuditConfig, Settings
from bystack.core.graph.model import Node
from bystack.core.identity import URN, NodeKind, container_urn
from bystack.core.ports.command import (
    AuditEntry,
    CommandKind,
    CommandStatus,
    TargetOutcome,
)
from bystack.infra.audit import durable
from bystack.infra.audit.durable import DurableAuditLog
from bystack.infra.audit.memory import InMemoryAuditLog

WEB = container_urn("e1", "c" * 64)
DB = container_urn("e1", "d" * 64)


def attempt(entry_id: str, kind: CommandKind = CommandKind.RESTART) -> AuditEntry:
    """What is written *before* dispatch. Deliberately incomplete."""
    return AuditEntry(
        id=entry_id,
        at=1_700_000_000.5,
        actor="anonymous",
        kind=kind,
        target=WEB,
        targets=(WEB, DB),
        status=CommandStatus.IN_FLIGHT,
    )


def completed(entry_id: str, status: CommandStatus = CommandStatus.SUCCEEDED) -> AuditEntry:
    return AuditEntry(
        id=entry_id,
        at=1_700_000_000.5,
        actor="anonymous",
        kind=CommandKind.RESTART,
        target=WEB,
        targets=(WEB, DB),
        status=status,
        detail="2 of 2 restarted",
        duration_ms=1234,
        outcomes=(
            TargetOutcome(WEB, CommandStatus.SUCCEEDED, None),
            TargetOutcome(DB, CommandStatus.SUCCEEDED, "already running"),
        ),
    )


@pytest.fixture
def audit(tmp_path: Path) -> DurableAuditLog:
    return DurableAuditLog.open(tmp_path / "state")


# --------------------------------------------------------------------------
# Durability, which is the whole point
# --------------------------------------------------------------------------


async def test_an_operation_survives_the_process_that_recorded_it(tmp_path: Path) -> None:
    """The one property that separates this from the ring it replaces, and the
    one ADR-0012 puts in front of every destructive verb: a log a restart
    erases cannot answer "who did this"."""
    first = DurableAuditLog.open(tmp_path / "state")
    await first.record(attempt("op-1"))
    await first.finalize("op-1", completed("op-1"))

    reopened = DurableAuditLog.open(tmp_path / "state")

    assert [e.id for e in reopened.recent()] == ["op-1"]
    assert reopened.recent()[0].status is CommandStatus.SUCCEEDED


async def test_an_operation_that_never_finished_is_still_on_disk(tmp_path: Path) -> None:
    """Written before dispatch, which is what makes a command that hung the
    process leave evidence. A log that recorded only completions cannot answer
    the one question it is ever asked during an incident: what was tried."""
    first = DurableAuditLog.open(tmp_path / "state")
    await first.record(attempt("op-1"))
    # No finalize. The process died here.

    reopened = DurableAuditLog.open(tmp_path / "state")

    assert reopened.recent()[0].status is CommandStatus.IN_FLIGHT
    assert reopened.recent()[0].kind is CommandKind.RESTART


async def test_the_finished_form_wins_over_the_attempt(audit, tmp_path: Path) -> None:
    """`finalize` appends rather than rewriting, so the file holds two records
    for one operation. Reading must collapse them to the later one, or every
    completed command would appear twice — once as in-flight, forever."""
    await audit.record(attempt("op-1"))
    await audit.finalize("op-1", completed("op-1", CommandStatus.FAILED))

    lines = (tmp_path / "state" / "operations.jsonl").read_text().splitlines()
    reopened = DurableAuditLog.open(tmp_path / "state")

    assert len(lines) == 2, "append-only: the attempt is not overwritten"
    assert len(reopened.recent()) == 1
    assert reopened.recent()[0].status is CommandStatus.FAILED


async def test_everything_worth_asking_about_survives_the_round_trip(audit, tmp_path: Path) -> None:
    """Serialized field by field rather than by reflection, so this is the
    check that a field added to `AuditEntry` was a deliberate change to the
    on-disk format."""
    await audit.record(completed("op-1"))

    entry = DurableAuditLog.open(tmp_path / "state").recent()[0]

    assert entry.actor == "anonymous"
    assert entry.at == 1_700_000_000.5
    assert entry.target == WEB
    assert entry.targets == (WEB, DB)
    assert entry.detail == "2 of 2 restarted"
    assert entry.duration_ms == 1234
    # The per-target outcomes are the part an operator reads when a stack
    # restart half worked.
    assert [(o.target, o.status, o.detail) for o in entry.outcomes] == [
        (WEB, CommandStatus.SUCCEEDED, None),
        (DB, CommandStatus.SUCCEEDED, "already running"),
    ]


async def test_refusals_are_recorded_too(audit, tmp_path: Path) -> None:
    """A read-only control plane that declined to restart a dead service is
    the single most useful line this log can hold. A log of only what
    succeeded would omit it."""
    await audit.record(
        AuditEntry(
            id="op-1",
            at=1.0,
            actor="anonymous",
            kind=CommandKind.STOP,
            target=WEB,
            status=CommandStatus.REJECTED,
            reason="read_only",
        )
    )

    entry = DurableAuditLog.open(tmp_path / "state").recent()[0]

    assert entry.status is CommandStatus.REJECTED
    assert entry.reason == "read_only"


# --------------------------------------------------------------------------
# Retention (ADR-0006 clause 4)
# --------------------------------------------------------------------------


async def test_the_log_is_bounded_and_compacts_itself(tmp_path: Path) -> None:
    """A bound that is only written down has already been exceeded. The
    trigger is twice the retention so compaction is amortised — a busy hour
    must not pay for a full rewrite on every operation."""
    audit = DurableAuditLog.open(tmp_path / "state", retain=10)
    for index in range(50):
        await audit.record(attempt(f"op-{index}"))

    lines = (tmp_path / "state" / "operations.jsonl").read_text().splitlines()

    assert len(audit) == 10
    assert len(lines) <= 20, "the file must not grow without bound"
    # And the survivors are the newest, not an arbitrary ten.
    assert [e.id for e in audit.recent(3)] == ["op-49", "op-48", "op-47"]


async def test_what_compaction_kept_is_what_a_restart_finds(tmp_path: Path) -> None:
    """A compaction that wrote a file the loader disagrees with would pass
    every in-process assertion above and lose the log on the next start."""
    audit = DurableAuditLog.open(tmp_path / "state", retain=10)
    for index in range(50):
        await audit.record(attempt(f"op-{index}"))

    reopened = DurableAuditLog.open(tmp_path / "state", retain=10)

    assert [e.id for e in reopened.recent(100)] == [e.id for e in audit.recent(100)]


def test_a_zero_retention_is_refused_rather_than_silently_meaning_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        DurableAuditLog.open(tmp_path / "state", retain=0)


# --------------------------------------------------------------------------
# The write path: durable, and not on the event loop
# --------------------------------------------------------------------------


#: Long enough to be unambiguous against scheduling noise, short enough that
#: five of them do not slow the suite down noticeably.
SLOW_FSYNC = 0.05


async def test_a_slow_disk_does_not_stop_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The defect this file's write path was rewritten for.

    `CommandService.execute` is a coroutine, so an fsync inside `record` runs
    on the thread that also drives every agent's WebSocket pump and every
    browser's delta stream. It was invisible to this suite by construction:
    `tmp_path` is tmpfs, where an fsync is 0.01 ms. Slowing it down on purpose
    is the only way to see what NVMe (0.78 ms) and a spinning disk (tens of
    milliseconds) actually do.

    Measured as the longest gap between two consecutive turns of the loop
    rather than as a throughput number, because the stall *is* the defect --
    a Controller that pumps the same number of frames a second while going
    deaf for 50 ms at a time is the thing being ruled out.

    The final `sleep(0)` is load-bearing. A blocking `record` never yields, so
    an implementation with the fsync back on the loop starves the watcher
    completely and it would have no samples at all to be wrong about; that
    last turn is what lets it take one sample spanning the whole blocked
    stretch, and turns "measured nothing" into a number.
    """
    audit = DurableAuditLog.open(tmp_path / "state")
    real_fsync = os.fsync
    monkeypatch.setattr(
        durable.os, "fsync", lambda fd: (time.sleep(SLOW_FSYNC), real_fsync(fd))[1]
    )

    gaps: list[float] = []

    async def watch() -> None:
        previous = time.perf_counter()
        while True:
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append(now - previous)
            previous = now

    watcher = asyncio.create_task(watch())
    for _ in range(3):
        await asyncio.sleep(0)  # prime it, so `gaps` is a baseline and not empty

    for index in range(5):
        await audit.record(attempt(f"op-{index}"))

    await asyncio.sleep(0)  # one more sample, spanning however long that took
    watcher.cancel()

    assert gaps, "the watcher never ran, so this measured nothing"
    assert max(gaps) < SLOW_FSYNC / 2, (
        f"the loop stalled for {max(gaps) * 1000:.0f}ms across {len(gaps)} turn(s); "
        f"the fsync is back on it"
    )
    # And it is still durable: moving the write off the loop must not have
    # turned it into a write that has not happened yet when `record` returns.
    assert len((tmp_path / "state" / "operations.jsonl").read_text().splitlines()) == 5


async def test_the_records_land_in_the_order_they_were_made(tmp_path: Path) -> None:
    """One lock, so concurrent writers serialise rather than interleave.

    Two coroutines recording at once is the ordinary case the moment a fleet
    has two operators, and a worker thread per call with no lock would let the
    second overtake the first — leaving a file whose order is not the order
    the operations happened in, which is most of what an audit log is for.
    """
    audit = DurableAuditLog.open(tmp_path / "state")

    await asyncio.gather(*(audit.record(attempt(f"op-{index}")) for index in range(20)))

    written = [
        json.loads(line)["id"]
        for line in (tmp_path / "state" / "operations.jsonl").read_text().splitlines()
    ]
    assert written == [f"op-{index}" for index in range(20)]


async def test_compaction_survives_a_record_arriving_during_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The compaction snapshot is taken on the loop, not in the worker.

    `_remember` runs on the event loop *before* `_persist` queues for the
    write lock, so a `record` issued while a compaction is in the worker
    mutates the mapping that worker is walking. Iterating the live one would
    meet "dictionary changed size during iteration" — on a busy Controller
    only, which is the worst schedule for a bug to keep.

    The pass over ten entries is microseconds, so a concurrent record would
    essentially never land in it and the check would be decoration. Slowing
    the per-entry serialization to 2 ms widens the window to ~20 ms, which is
    a window a test can aim at.
    """
    audit = DurableAuditLog.open(tmp_path / "state", retain=10)
    for index in range(20):
        await audit.record(attempt(f"op-{index}"))  # one short of the trigger

    real_as_json = durable._as_json
    monkeypatch.setattr(
        durable, "_as_json", lambda entry: (time.sleep(0.002), real_as_json(entry))[1]
    )

    compacting = asyncio.create_task(audit.record(attempt("op-20")))
    await asyncio.sleep(0.006)  # the worker is now part-way through the snapshot
    await audit.record(attempt("op-21"))  # `_remember` adds a key, on the loop
    await compacting

    lines = (tmp_path / "state" / "operations.jsonl").read_text().splitlines()
    assert len(lines) <= 20
    assert [e.id for e in audit.recent(2)] == ["op-21", "op-20"]
    assert [e.id for e in DurableAuditLog.open(tmp_path / "state", retain=10).recent(100)] == [
        e.id for e in audit.recent(100)
    ]


# --------------------------------------------------------------------------
# Failing safely
# --------------------------------------------------------------------------


async def test_one_unreadable_line_does_not_cost_the_rest_of_the_log(tmp_path: Path) -> None:
    """Deliberately the opposite of the enrollment registry's rule. An
    unreadable allow-list means we do not know who is approved and every way
    to proceed is an outage; an unreadable audit line means one record of the
    past is gone, and refusing to start over it turns a corrupted log into a
    control plane that will not run."""
    audit = DurableAuditLog.open(tmp_path / "state")
    await audit.record(attempt("op-1"))
    await audit.record(attempt("op-2"))

    path = tmp_path / "state" / "operations.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], "{not json at all", lines[1]]) + "\n")

    reopened = DurableAuditLog.open(tmp_path / "state")

    assert {e.id for e in reopened.recent()} == {"op-1", "op-2"}


async def test_a_log_it_cannot_write_does_not_fail_the_command(tmp_path: Path) -> None:
    """`AuditLog.record` is documented never to fail a command by failing
    itself, and that holds even here: a full disk must not stop an operator
    restarting the service that filled it."""
    audit = DurableAuditLog.open(tmp_path / "state")
    # A directory where the file should be: unopenable for append, and a
    # stand-in for every OSError this can meet, including the full disk.
    (tmp_path / "state" / "operations.jsonl").mkdir()

    await audit.record(attempt("op-1"))

    # Recorded in memory, so the running process can still answer; simply not
    # durable, which is what the log line at ERROR is for.
    assert [e.id for e in audit.recent()] == ["op-1"]


async def test_the_log_is_not_world_readable(tmp_path: Path) -> None:
    """It names every operation attempted against every managed host. The
    directory is 0700 and the file 0600 for the same reason the CA key is."""
    audit = DurableAuditLog.open(tmp_path / "state")
    await audit.record(attempt("op-1"))

    assert (tmp_path / "state").stat().st_mode & 0o077 == 0
    assert (tmp_path / "state" / "operations.jsonl").stat().st_mode & 0o077 == 0


async def test_the_file_is_one_json_object_per_line(audit, tmp_path: Path) -> None:
    """JSON Lines rather than a JSON array, so appending is a write and not a
    read-modify-write of the whole history. `jq` and `grep` both work on it,
    which is most of the argument for a file at all."""
    await audit.record(attempt("op-1"))
    await audit.record(attempt("op-2"))

    for line in (tmp_path / "state" / "operations.jsonl").read_text().splitlines():
        assert isinstance(json.loads(line), dict)


# --------------------------------------------------------------------------
# The in-memory form still behaves
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Through the composition root, which is where this is actually chosen
# --------------------------------------------------------------------------


def test_an_operation_outlives_the_controller_that_refused_it(tmp_path: Path) -> None:
    """The whole feature, through the real wiring.

    A refusal rather than a success, because it is the line this log exists
    for: a read-only control plane that declined to restart a dead service is
    the single most useful record it can hold, and the in-memory version lost
    it on every restart.
    """
    settings = Settings(agents=AgentsConfig(state_dir=str(tmp_path)), read_only=True)
    urn = str(container_urn("e1", "c" * 64))

    with TestClient(create_app(settings)) as first:
        _seed(first.app, urn)
        refused = first.post(f"{API_PREFIX}/commands", json={"kind": "stop", "target": urn})
        assert refused.status_code == 403

    # A different process, in every sense that matters here: a new context, a
    # new store, a new audit object. Only the directory is shared.
    with TestClient(create_app(settings)) as second:
        body = second.get(f"{API_PREFIX}/commands/audit").json()

    assert [(e["kind"], e["status"], e["target"]) for e in body] == [("stop", "rejected", urn)]
    # `reason` is the operator's stated justification and there was none here;
    # why the platform said no is in `detail`, and it is the part that had to
    # survive.
    assert body[0]["detail"].startswith("read_only")


def test_an_operator_can_turn_durability_off(tmp_path: Path) -> None:
    """It is a default, not a law. Somebody running this on a read-only root
    or in a container with no writable volume gets the bounded ring and a
    Controller that starts."""
    settings = Settings(
        agents=AgentsConfig(state_dir=str(tmp_path)),
        audit=AuditConfig(durable=False),
    )

    with TestClient(create_app(settings)) as client:
        client.get(f"{API_PREFIX}/commands/audit")

    assert not (tmp_path / "operations.jsonl").exists()


def _seed(app: Any, urn: str) -> None:
    app.state.context.store.upsert(
        "e1",
        [
            Node(
                urn=URN(urn),
                kind=NodeKind.CONTAINER,
                name="web",
                source="e1",
                status="exited",
            )
        ],
    )


# --------------------------------------------------------------------------
# The in-memory form still behaves
# --------------------------------------------------------------------------


async def test_the_in_memory_ring_is_still_bounded_and_newest_first() -> None:
    audit = InMemoryAuditLog(capacity=3)
    for index in range(5):
        await audit.record(attempt(f"op-{index}"))

    assert [e.id for e in audit.recent()] == ["op-4", "op-3", "op-2"]


async def test_both_implementations_answer_recent_the_same_way(tmp_path: Path) -> None:
    """They are two implementations of one port, and the UI reads whichever it
    is given. A durable log that ordered its answers the other way round would
    reverse the timeline the day an operator most needed it."""
    ring = InMemoryAuditLog(capacity=10)
    disk = DurableAuditLog.open(tmp_path / "state", retain=10)
    for index in range(5):
        await ring.record(attempt(f"op-{index}"))
        await disk.record(attempt(f"op-{index}"))

    assert [e.id for e in ring.recent(3)] == [e.id for e in disk.recent(3)]
    assert ring.recent(0) == disk.recent(0) == ()
