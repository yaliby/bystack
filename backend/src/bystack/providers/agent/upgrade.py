"""Pushing one release to one agent (ADR-0017).

The Controller's half of a transfer, and it is deliberately thin: it sends a
signed document it cannot make, then the bytes that document describes, and
believes whatever the agent says about both. Every decision that matters --
whether the signature is good, whether the version is higher, whether the
architecture is right -- is taken on the host against a key this process does
not have.

That asymmetry is what makes the channel simple. There is no retry policy
because a refusal is not retryable (a bad signature does not improve), and no
partial-success state because staging is atomic at the far end: the agent
writes a trigger file only after the artifact is complete and its digest
matches.

**Chunked, and not for memory.** A 2.2 MB artifact would fit in one frame
against the agent's 64 MB ceiling. It is chunked because this socket also
carries the live graph, and a multi-megabyte burst on it is a topology map that
stops moving for the duration of every upgrade -- on the Controller's side as
much as the agent's, since one send of two megabytes is one send the other
hosts' deltas queue behind.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Final

from bystack.agent.v1 import agent_pb2 as wire
from bystack.core.ports.agent import AgentDisconnected, AgentSession
from bystack.infra.releases import Release

log = logging.getLogger(__name__)

#: The capability an agent advertises when it can be upgraded over the wire.
#:
#: Absent from a build compiled with no release signing keys, and absent on the
#: Controller's own local agent -- which is a file inside the Controller's
#: installation, and upgrading *that* is upgrading the Controller. Absence is
#: the answer for all three of "too old", "no keys" and "not ours", and the
#: Controller's response is the same in each: say so, and send nothing.
CAP_UPGRADE: Final = "upgrade"

#: Bytes per frame.
#:
#: Small enough that a chunk interleaves with a delta rather than delaying it,
#: large enough that a 2.2 MB agent is 34 frames rather than 2,200. Nothing on
#: either side depends on the value: the agent checks offsets and totals, so
#: this can move without a protocol change.
CHUNK_BYTES: Final = 64 * 1024

#: A yield between chunks, so a transfer cannot monopolise the connection.
#:
#: The point of chunking, made real. Without this the loop below sends every
#: frame it has as fast as the socket accepts them, which is exactly the
#: multi-megabyte burst the chunking exists to avoid -- the frames are merely
#: smaller. Five milliseconds puts a 2.2 MB transfer at roughly two seconds and
#: leaves the graph moving throughout, which is the trade this feature is
#: allowed to make: nobody is watching an upgrade complete, and everybody is
#: watching the map.
CHUNK_PAUSE: Final = 0.005

#: How long to wait for the agent to accept or refuse an offer.
#:
#: One local decision -- a signature check and a version comparison -- plus a
#: round trip over whatever uplink the host has. Longer than a log read's
#: deadline because a host that has been asleep may be answering its first
#: frame in hours.
OFFER_TIMEOUT: Final = 30.0

#: How long to wait for `staged` after the last chunk.
#:
#: The agent has to hash a few megabytes and rename a file. Generous, because
#: the alternative to waiting is reporting a failure for an upgrade that then
#: installs itself anyway.
STAGE_TIMEOUT: Final = 60.0


@dataclass(frozen=True, slots=True)
class UpgradeOutcome:
    """What one host did with one release.

    `state` is the agent's own word — `staged`, `refused`, `failed` — kept
    rather than flattened into a boolean, because the three call for different
    things from an operator. A refusal is final and names something about the
    release; a failure is about the host and is worth trying again; `staged`
    means the host is about to disconnect and come back, which is not an error
    even though it looks like one from here.
    """

    state: str
    reason: str | None = None
    sent: int = 0

    @property
    def staged(self) -> bool:
        return self.state == "staged"


class UpgradeChannel:
    """One transfer at a time, for one agent connection.

    Bound to the connection like every other correlated exchange here. A
    reconnect gets a fresh channel, which is correct: the *partial file* on the
    host survives and is resumed by the next offer, but a status arriving on a
    new connection for a transfer made on the old one is an answer to a
    question this stream never asked.
    """

    __slots__ = ("_statuses",)

    def __init__(self) -> None:
        #: transfer_id -> the statuses the agent has sent for it. A queue
        #: rather than a future, because one transfer produces at least two:
        #: an acceptance, then a terminal state. A future resolved twice is an
        #: exception in the receive loop.
        self._statuses: dict[str, asyncio.Queue[wire.UpgradeStatus]] = {}

    def __len__(self) -> int:
        return len(self._statuses)

    async def push(self, session: AgentSession, release: Release) -> UpgradeOutcome:
        """Offer a release and, if the agent accepts, send it.

        Never raises for anything the agent or the network does. The caller is
        a rollout that has to keep a record per host, and an exception there is
        a run that stops with no row explaining which host it stopped on.
        """
        transfer_id = uuid.uuid4().hex[:16]
        queue: asyncio.Queue[wire.UpgradeStatus] = asyncio.Queue()
        self._statuses[transfer_id] = queue
        try:
            return await self._transfer(session, release, transfer_id, queue)
        except AgentDisconnected as exc:
            return UpgradeOutcome("failed", str(exc))
        except TimeoutError:
            return UpgradeOutcome(
                "failed",
                "the agent stopped answering during the transfer; nothing was installed",
            )
        finally:
            # Unconditional, on every path out. A fleet under churn would
            # otherwise accumulate one queue per abandoned transfer.
            self._statuses.pop(transfer_id, None)

    async def _transfer(
        self,
        session: AgentSession,
        release: Release,
        transfer_id: str,
        queue: asyncio.Queue[wire.UpgradeStatus],
    ) -> UpgradeOutcome:
        artifact = await asyncio.to_thread(release.read)
        await session.send(
            wire.Envelope(
                upgrade_offer=wire.UpgradeOffer(
                    transfer_id=transfer_id,
                    # Verbatim, never rebuilt. What the agent verifies is the
                    # byte string that was signed.
                    manifest=release.manifest,
                    signature=release.signature,
                    total_bytes=len(artifact),
                )
            )
        )

        async with asyncio.timeout(OFFER_TIMEOUT):
            first = await queue.get()
        if first.state != "accepted":
            return UpgradeOutcome(first.state or "refused", first.reason or None)

        # Where the agent already is, from a transfer a reconnect interrupted.
        # Trusted because it costs nothing to: the agent checks every offset
        # against its own file and refuses one that does not continue it, so a
        # wrong number here is a failed transfer rather than a spliced binary.
        offset = min(first.resume_from, len(artifact))
        if offset:
            log.info("agent %s already holds %d bytes of this release", session.engine_id, offset)

        while offset < len(artifact):
            block = artifact[offset : offset + CHUNK_BYTES]
            offset += len(block)
            await session.send(
                wire.Envelope(
                    upgrade_chunk=wire.UpgradeChunk(
                        transfer_id=transfer_id,
                        offset=offset - len(block),
                        data=block,
                        last=offset >= len(artifact),
                    )
                )
            )
            # A failure mid-transfer is answered immediately by the agent, and
            # continuing to push two megabytes at a host that has already said
            # no is the one thing worth checking for between frames.
            if not queue.empty():
                status = queue.get_nowait()
                return UpgradeOutcome(status.state or "failed", status.reason or None, offset)
            await asyncio.sleep(CHUNK_PAUSE)

        async with asyncio.timeout(STAGE_TIMEOUT):
            final = await queue.get()
        return UpgradeOutcome(final.state or "failed", final.reason or None, offset)

    def resolve(self, status: wire.UpgradeStatus) -> None:
        """Route one status to whoever is pushing. Never raises.

        Called from the receive loop. A status for a transfer we are not
        holding is ordinary -- it is what a reconnect mid-transfer produces --
        and tearing down a healthy connection over it would turn a race into an
        outage.
        """
        queue = self._statuses.get(status.transfer_id)
        if queue is None:
            log.debug("upgrade status for unknown transfer %s; ignoring", status.transfer_id)
            return
        queue.put_nowait(status)

    def abandon(self, reason: str) -> None:
        """Fail every push. Called when the connection drops.

        The pusher is waiting on a queue with a deadline attached, so this is
        what turns a disconnect into an immediate answer instead of a rollout
        that sits out a minute per host for a reply that provably cannot
        arrive. A synthesized status rather than an exception, because the
        distinction between "the host refused" and "the host went away" is one
        the outcome already carries.
        """
        for transfer_id, queue in list(self._statuses.items()):
            queue.put_nowait(
                wire.UpgradeStatus(transfer_id=transfer_id, state="failed", reason=reason)
            )
        self._statuses.clear()
