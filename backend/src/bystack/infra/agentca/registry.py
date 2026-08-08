"""Who is enrolled, and who is allowed to connect.

This is the allow-list ADR-0011 chose instead of a CRL. With an authoritative
record of every agent we ever issued to, revocation is a lookup we already
have to perform at connection time; CRL machinery would be ceremony around it.

**This file is durable, and it is meant to be.** ADR-0001 permits exactly four
categories of persistent state, and enrollment records are category two --
user intent (this host is approved) plus certificate metadata. It is the only
thing in the Controller that survives a restart, and it has to be: an operator
who approved forty hosts last month did not consent to doing it again because
the process was restarted. The graph itself remains ephemeral and rebuildable,
exactly as before.

A JSON file rather than a table because it is a few hundred bytes that change
when a human approves a host, and Postgres is not in this tree yet. The shape
is the one a table would have, so moving it is a change to this module.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_FILE = "agents.json"

#: How many certificate serials stay valid per agent.
#:
#: Two: the current one and the one it replaced. Renewal hands an agent a new
#: certificate over the live stream, and the agent starts using it on its next
#: connection -- but if it fails to persist the new one, or is restarted
#: between receiving and writing it, it comes back with the old one. Accepting
#: only the newest serial would lock that host out permanently and require a
#: person to visit it, for a failure that happens unattended every sixty days.
_SERIALS_KEPT = 2


class AgentStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVOKED = "revoked"


class Admission(Enum):
    """The answer to "may this certificate connect?"."""

    ADMITTED = "admitted"
    UNKNOWN = "unknown"
    PENDING = "awaiting approval"
    REVOKED = "revoked"
    SUPERSEDED = "certificate superseded by a later enrollment"


@dataclass(frozen=True, slots=True)
class EnrolledAgent:
    engine_id: str
    status: AgentStatus
    serials: tuple[int, ...]
    not_after: int
    enrolled_at: int
    agent_version: str = ""
    last_seen: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "status": self.status.value,
            # Serials are 128-bit and go out as strings. A JSON number that
            # large is legal and survives no round trip anyone can rely on.
            "serials": [str(serial) for serial in self.serials],
            "not_after": self.not_after,
            "enrolled_at": self.enrolled_at,
            "agent_version": self.agent_version,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> EnrolledAgent:
        return cls(
            engine_id=str(raw["engine_id"]),
            status=AgentStatus(str(raw["status"])),
            serials=tuple(int(serial) for serial in raw.get("serials", ())),
            not_after=int(raw.get("not_after", 0)),
            enrolled_at=int(raw.get("enrolled_at", 0)),
            agent_version=str(raw.get("agent_version", "")),
            last_seen=int(raw.get("last_seen", 0)),
        )


@dataclass(slots=True)
class EnrollmentRegistry:
    """Every agent we have issued a certificate to, and its standing."""

    path: Path
    _agents: dict[str, EnrolledAgent] = field(default_factory=dict)

    @classmethod
    def open(cls, directory: str | os.PathLike[str]) -> EnrollmentRegistry:
        path = Path(directory).expanduser() / _FILE
        registry = cls(path)
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                registry._agents = {
                    str(entry["engine_id"]): EnrolledAgent.from_json(entry) for entry in raw
                }
            except (ValueError, KeyError, TypeError) as exc:
                # Loud, and fatal. An unreadable enrollment file means we do
                # not know who is approved, and the two ways to proceed are
                # "admit nobody" (a silent fleet-wide outage) and "admit
                # everybody" (the thing this file exists to prevent).
                raise ValueError(f"unreadable enrollment registry {path}: {exc}") from exc
        return registry

    # -- enrollment -------------------------------------------------------

    def enroll(
        self,
        engine_id: str,
        *,
        serial: int,
        not_after: int,
        agent_version: str,
        auto_approve: bool,
    ) -> EnrolledAgent:
        """Record a certificate we just issued against a redeemed token.

        A re-enrollment (a host that lost its key, or was rebuilt with the
        same engine) drops every previous serial. That is the difference
        between enrollment and renewal: renewal extends an identity that is
        connected and authenticated, enrollment establishes one from a token,
        and a fresh token is the operator saying the old credential should
        stop working.

        An agent that was revoked stays revoked. A token is not an appeal:
        whoever holds the revoked host's shell can also mint a valid enrollment
        if the operator has one outstanding, and the revocation would quietly
        undo itself.
        """
        now = _now()
        previous = self._agents.get(engine_id)
        if previous is not None and previous.status is AgentStatus.REVOKED:
            status = AgentStatus.REVOKED
        elif previous is not None and previous.status is AgentStatus.APPROVED:
            # Already approved once. Re-enrolling the same host is a
            # credential change, not a new host, and asking for approval again
            # would make key rotation an interactive operation.
            status = AgentStatus.APPROVED
        else:
            status = AgentStatus.APPROVED if auto_approve else AgentStatus.PENDING

        agent = EnrolledAgent(
            engine_id=engine_id,
            status=status,
            serials=(serial,),
            not_after=not_after,
            enrolled_at=previous.enrolled_at if previous else now,
            agent_version=agent_version,
            last_seen=previous.last_seen if previous else 0,
        )
        self._put(agent)
        return agent

    def renewed(self, engine_id: str, *, serial: int, not_after: int) -> None:
        """Record a certificate issued over an already-authenticated stream."""
        agent = self._agents.get(engine_id)
        if agent is None:
            return
        serials = (serial, *agent.serials)[:_SERIALS_KEPT]
        self._put(replace(agent, serials=serials, not_after=not_after))

    # -- connection time --------------------------------------------------

    def admit(self, engine_id: str, serial: int) -> Admission:
        """May this certificate carry a connection right now?

        Called on every connection, which is what makes revocation take effect
        without a CRL, a push, or a wait.
        """
        agent = self._agents.get(engine_id)
        if agent is None:
            return Admission.UNKNOWN
        if agent.status is AgentStatus.REVOKED:
            return Admission.REVOKED
        if serial not in agent.serials:
            return Admission.SUPERSEDED
        if agent.status is AgentStatus.PENDING:
            return Admission.PENDING
        return Admission.ADMITTED

    def seen(self, engine_id: str, *, agent_version: str = "") -> None:
        agent = self._agents.get(engine_id)
        if agent is None:
            return
        self._put(
            replace(agent, last_seen=_now(), agent_version=agent_version or agent.agent_version)
        )

    # -- operator ---------------------------------------------------------

    def approve(self, engine_id: str) -> EnrolledAgent | None:
        return self._set_status(engine_id, AgentStatus.APPROVED)

    def revoke(self, engine_id: str) -> EnrolledAgent | None:
        return self._set_status(engine_id, AgentStatus.REVOKED)

    def get(self, engine_id: str) -> EnrolledAgent | None:
        return self._agents.get(engine_id)

    def all(self) -> list[EnrolledAgent]:
        return sorted(self._agents.values(), key=lambda agent: agent.engine_id)

    def _set_status(self, engine_id: str, status: AgentStatus) -> EnrolledAgent | None:
        agent = self._agents.get(engine_id)
        if agent is None:
            return None
        updated = replace(agent, status=status)
        self._put(updated)
        return updated

    # -- persistence ------------------------------------------------------

    def _put(self, agent: EnrolledAgent) -> None:
        self._agents[agent.engine_id] = agent
        self._flush()

    def _flush(self) -> None:
        """Write through, atomically.

        Every mutation, because there are a handful a day and losing the one
        that mattered -- a revocation -- is the failure this file exists to
        prevent. Rename rather than truncate-and-write: a Controller killed
        mid-write must not come back to a half-written allow-list, which
        `open` treats as fatal and would turn a crash into a fleet outage.
        """
        temporary = self.path.with_suffix(".json.tmp")
        payload = json.dumps([agent.as_json() for agent in self.all()], indent=2)
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, self.path)


def _now() -> int:
    return int(dt.datetime.now(dt.UTC).timestamp())
