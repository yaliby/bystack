"""A scripted Docker Engine on a unix socket.

Point an agent at this instead of a real daemon and it cannot tell the
difference for the subset it uses. That is what makes the agent testable at
all: the interesting cases -- a container that vanishes between the event and
the List, a burst of a hundred events in one tick, a daemon that closes the
event stream mid-watch -- are not reproducible against a real daemon on
demand, and two of them are the exact bugs the informer exists to prevent.

It also removes the last excuse for a test needing Docker. The rule in this
repo is that no test anywhere requires a daemon or a network, and an agent is
the one component that genuinely must talk to a socket. So we give it one.

Implemented against ``asyncio.start_unix_server`` with hand-written HTTP/1.1
rather than a framework, for one reason: the response shapes have to be
byte-comparable to Docker's, including the chunked event stream, and a
framework that helpfully normalises them would hide precisely the framing bugs
this is here to catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger(__name__)


@dataclass
class Recorded:
    """What the agent asked for, so a test can assert on how much it talks.

    The resource budget is a requirement rather than an aspiration, so call
    counts are worth asserting on: an agent that re-Lists twenty times for one
    ``compose up`` passes every behavioural test and fails the one that
    matters.
    """

    calls: list[str] = field(default_factory=list)
    actions: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def count(self, path: str) -> int:
        return sum(1 for call in self.calls if call.startswith(path))


class ScriptedEngine:
    """A Docker daemon whose contents a test controls."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        info: dict[str, Any] | None = None,
        containers: Sequence[dict[str, Any]] = (),
        networks: Sequence[dict[str, Any]] = (),
        volumes: Sequence[dict[str, Any]] = (),
        images: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.path = str(socket_path)
        self.info = info or DEFAULT_INFO
        self.containers = list(containers)
        self.networks = list(networks)
        self.volumes = list(volumes)
        self.images = list(images)
        #: Container id -> the lines it has written, as ``(stderr, text)``.
        #:
        #: Held per container rather than globally so that asking the wrong
        #: one is a visible failure rather than a coincidence that passes.
        self.logs: dict[str, list[tuple[bool, str]]] = {}
        self.recorded = Recorded()
        #: Seconds an inspect takes to answer. Zero, except where a check is
        #: about *how* the agent issues them: a real daemon answers a local
        #: inspect in well under a millisecond, so thirty-two of them cost the
        #: same whether they were issued one after another or all at once, and
        #: a suite that never slows one down cannot tell those apart.
        self.inspect_delay = 0.0
        #: Container ids currently being followed. What lets a check assert
        #: that a cancel actually reached the daemon rather than merely being
        #: sent -- an agent that forgot to drop the connection leaves an entry
        #: here forever, which is the leak the cap exists to bound.
        self.following: set[str] = set()
        self._ended_streams: set[str] = set()

        self._server: asyncio.AbstractServer | None = None
        self._event_streams: set[asyncio.StreamWriter] = set()
        #: Set once a watcher has attached, so a test can emit events without
        #: racing the agent's startup.
        self.watching = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> ScriptedEngine:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        self._server = await asyncio.start_unix_server(self._serve, path=self.path)
        return self

    async def stop(self) -> None:
        for writer in list(self._event_streams):
            writer.close()
        self._event_streams.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    async def __aenter__(self) -> ScriptedEngine:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- driving the scenario ---------------------------------------------

    async def emit(self, kind: str, action: str = "start") -> None:
        """Push one event to every attached watcher."""
        line = json.dumps({"Type": kind, "Action": action, "time": 0}).encode() + b"\n"
        for writer in list(self._event_streams):
            try:
                writer.write(b"%x\r\n%s\r\n" % (len(line), line))
                await writer.drain()
            except (ConnectionError, RuntimeError):
                self._event_streams.discard(writer)

    async def drop_event_stream(self) -> None:
        """Close the watch without closing the socket.

        A daemon restart, as the agent experiences it. The agent must notice
        and re-List rather than sitting on a dead stream believing the host is
        idle -- which is indistinguishable from a healthy quiet host, and is
        why this needs a test.
        """
        for writer in list(self._event_streams):
            writer.close()
        self._event_streams.clear()

    # -- HTTP --------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readline()
            if not request:
                return
            method, raw_target, _ = request.decode().split(" ", 2)

            # Drain the headers. We never read a body: every endpoint the
            # agent uses is a GET, or a POST whose parameters are in the query
            # string -- which is Docker's own convention, not ours.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            target = urlsplit(raw_target)
            path = unquote(target.path)
            query = parse_qs(target.query)
            self.recorded.calls.append(path)

            await self._route(method, path, query, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            if writer not in self._event_streams:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    writer.close()

    async def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if path == "/events":
            await self._stream_events(query, writer)
            return

        if method == "POST":
            await self._act(path, query, writer)
            return

        # Per-container GETs, matched on shape rather than prefix: the list
        # endpoint is `/containers/json`, so a `endswith("/json")` test would
        # route it here and inspect a container called "json".
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "containers":
            if parts[2] == "logs":
                await self._logs(parts[1], query, reader, writer)
                return
            if parts[2] == "json":
                await self._inspect(parts[1], writer)
                return

        match path:
            case "/info":
                await _json(writer, self.info)
            case "/containers/json":
                await _json(writer, self.containers)
            case "/networks":
                await _json(writer, self.networks)
            case "/volumes":
                await _json(writer, {"Volumes": self.volumes})
            case "/images/json":
                await _json(writer, self.images)
            case _:
                await _status(writer, 404, {"message": f"page not found: {path}"})

    async def _act(
        self, path: str, query: dict[str, list[str]], writer: asyncio.StreamWriter
    ) -> None:
        """`POST /containers/{id}/{verb}`, with Docker's status semantics."""
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "containers":
            await _status(writer, 404, {"message": "no such endpoint"})
            return

        _, container_id, verb = parts
        args = {key: values[0] for key, values in query.items()}
        self.recorded.actions.append((container_id, verb, args))

        container = next((c for c in self.containers if c["Id"] == container_id), None)
        if container is None:
            await _status(writer, 404, {"message": f"No such container: {container_id}"})
            return

        state = container.get("State")
        # 304, not 204, when the container is already in the requested state.
        # Docker is precise about this and an agent that flattens it would
        # report a restart that changed nothing as one that worked.
        already = (verb == "start" and state == "running") or (
            verb == "stop" and state == "exited"
        )
        if already:
            await _raw(writer, 304, b"")
            return

        if verb in ("pause", "unpause") and state != "running":
            await _status(writer, 409, {"message": f"container {container_id} is not running"})
            return

        container["State"] = {
            "start": "running",
            "restart": "running",
            "unpause": "running",
            "stop": "exited",
            "kill": "exited",
            "pause": "paused",
        }.get(verb, state)
        await _raw(writer, 204, b"")

    async def _logs(
        self,
        container_id: str,
        query: dict[str, list[str]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """``GET /containers/{id}/logs``, in Docker's multiplexed framing.

        The framing is the whole point of scripting this endpoint rather than
        returning plain text: an 8-byte header per chunk whose first byte is
        the stream (1 = stdout, 2 = stderr) and whose last four are the
        payload length, big-endian. An agent that returns lines but loses the
        tag passes every plain-text fixture and throws away most of the
        diagnostic value -- the line that explains a crash is almost always
        the one on stderr.

        ``tail`` is applied here, by the daemon, because that is what makes
        the answer bounded on a container that has been logging for a month.
        Docker applies it per stream; so does this.
        """
        if not any(c["Id"] == container_id for c in self.containers):
            await _status(writer, 404, {"message": f"No such container: {container_id}"})
            return

        lines = self.logs.get(container_id, [])
        tail = int(query.get("tail", [str(len(lines))])[0] or len(lines))

        if query.get("follow", ["0"])[0] in ("1", "true"):
            await self._follow_logs(container_id, tail, reader, writer)
            return

        # Tailed per stream and then restored to write order, which is what
        # the daemon does -- and the reason the agent's `demultiplex` has to
        # tail the merged result a second time.
        kept: set[int] = set()
        for stream in (False, True):
            indexes = [i for i, (s, _) in enumerate(lines) if s is stream]
            kept.update(indexes[-tail:])
        await _raw(writer, 200, b"".join(_frame(*lines[i]) for i in sorted(kept)))

    async def _follow_logs(
        self,
        container_id: str,
        tail: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """``GET /containers/{id}/logs?follow=1``: backfill, then keep going.

        Chunked transfer, one HTTP chunk per batch, because that is what the
        daemon does and because the agent's job is to reassemble frames that
        do not line up with chunk boundaries. A fixture that sent the whole
        thing at once would never exercise that.

        Ends two ways, and it needs both. `end_log_stream` is how a check
        drives "the container exited" without a container. **The reader going
        away** is the cancellation path, and it is watched explicitly rather
        than discovered on the next write: a quiet container never writes, so
        a loop that only noticed a broken pipe would keep following a log
        nobody is reading until something happened to say. That left the
        cancellation observable only on a container that was still producing
        output -- which is not the container an operator closes the panel on.
        """
        head = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        writer.write(head)

        sent = 0
        backfill = self.logs.get(container_id, [])[-tail:] if tail else []
        if backfill:
            writer.write(_chunked(b"".join(_frame(s, t) for s, t in backfill)))
            sent = len(self.logs.get(container_id, []))
        await writer.drain()
        self.following.add(container_id)

        # Completes on EOF, which is what the agent dropping its end of the
        # socket looks like from here. Nothing is ever sent on this connection
        # after the request, so anything at all arriving means the peer closed.
        departed = asyncio.ensure_future(reader.read(1))
        try:
            while container_id not in self._ended_streams and not departed.done():
                lines = self.logs.get(container_id, [])
                if len(lines) > sent:
                    fresh = lines[sent:]
                    sent = len(lines)
                    writer.write(_chunked(b"".join(_frame(s, t) for s, t in fresh)))
                    await writer.drain()
                await asyncio.sleep(0.05)
            if not departed.done():
                writer.write(b"0\r\n\r\n")  # the terminating chunk
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            # The agent cancelled by dropping the connection, which is exactly
            # what a `LogsCancel` is supposed to cause.
            pass
        finally:
            departed.cancel()
            self.following.discard(container_id)

    def end_log_stream(self, container_id: str) -> None:
        """Close a followed log, as a container exiting would."""
        self._ended_streams.add(container_id)

    async def _inspect(self, container_id: str, writer: asyncio.StreamWriter) -> None:
        """``GET /containers/{id}/json``, carrying `RestartCount` and nothing else.

        Deliberately not a faithful inspect response. The real one is the
        largest document the Engine API serves and an agent that started
        reading more of it would be building a second model of a container
        beside the listed one; a fixture that offered the whole thing would
        make that easy to do by accident.

        The call is recorded like every other, which is what lets a check
        assert that a steady host pays for no inspects at all.
        """
        container = next((c for c in self.containers if c["Id"] == container_id), None)
        if container is None:
            await _status(writer, 404, {"message": f"No such container: {container_id}"})
            return
        if self.inspect_delay:
            await asyncio.sleep(self.inspect_delay)
        await _json(writer, {"Id": container_id, "RestartCount": container.get("RestartCount", 0)})

    async def _stream_events(
        self, query: dict[str, list[str]], writer: asyncio.StreamWriter
    ) -> None:
        """The chunked `GET /events` stream.

        Held open forever, like a real quiet daemon. Returning here would make
        the agent treat the stream as closed and reconnect, which is a
        different scenario with its own test.
        """
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        await writer.drain()
        self._event_streams.add(writer)
        self.watching.set()

        # Park. The connection lives until `emit` fails, `drop_event_stream`
        # closes it, or the agent goes away.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()


def _chunked(payload: bytes) -> bytes:
    """One HTTP/1.1 chunk, which is how the daemon streams a followed log."""
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


def _frame(stderr: bool, text: str) -> bytes:
    """One chunk in Docker's multiplexed log framing.

    Byte 0 is the stream, bytes 1-3 are zero, bytes 4-8 are the payload
    length big-endian. The three zero bytes are what an agent uses to tell a
    framed body from a TTY container's raw one, so they are not padding.
    """
    payload = (text + "\n").encode()
    return bytes([2 if stderr else 1, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


async def _json(writer: asyncio.StreamWriter, payload: Any) -> None:
    await _raw(writer, 200, json.dumps(payload).encode())


async def _status(writer: asyncio.StreamWriter, code: int, payload: Any) -> None:
    await _raw(writer, code, json.dumps(payload).encode())


async def _raw(writer: asyncio.StreamWriter, code: int, body: bytes) -> None:
    reason = {200: "OK", 204: "No Content", 304: "Not Modified", 404: "Not Found",
              409: "Conflict"}.get(code, "OK")
    head = f"HTTP/1.1 {code} {reason}\r\n"
    # 204 and 304 carry no body by definition, and sending Content-Length on
    # them puts some clients into a state where they wait for one.
    if code not in (204, 304):
        head += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    head += "Connection: close\r\n\r\n"
    writer.write(head.encode() + body)
    with contextlib.suppress(ConnectionError, RuntimeError):
        await writer.drain()


DEFAULT_INFO: dict[str, Any] = {
    "ID": "CONF:ORMA:NCE1",
    "Name": "conformance-host",
    "ServerVersion": "29.6.0",
    "OperatingSystem": "Fedora Linux 44",
    "KernelVersion": "6.19.10",
    "Architecture": "x86_64",
    "NCPU": 8,
    "MemTotal": 16_000_000_000,
    "ContainersRunning": 1,
    "Containers": 1,
}


def make_container(
    container_id: str,
    name: str,
    *,
    state: str = "running",
    project: str | None = "shop",
    service: str | None = "web",
    image_id: str = "sha256:" + "a" * 64,
    volume: str | None = "shop_data",
    network_id: str = "net1",
) -> dict[str, Any]:
    """A container as `/containers/json` reports it."""
    labels: dict[str, str] = {}
    if project and service:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service
        labels["com.docker.compose.container-number"] = "1"

    mounts: list[dict[str, Any]] = [
        {"Type": "bind", "Source": "/etc/localtime", "Destination": "/etc/localtime",
         "Mode": "ro", "RW": False}
    ]
    if volume:
        mounts.append(
            {"Type": "volume", "Name": volume, "Destination": "/data", "Mode": "rw", "RW": True}
        )

    return {
        "Id": container_id,
        "Names": [f"/{name}"],
        "Image": "nginx:latest",
        "ImageID": image_id,
        "Command": "nginx -g 'daemon off;'",
        "Created": 1_700_000_000,
        "State": state,
        # The trap, present in every fixture so an agent that hashes it fails
        # the conformance run rather than merely costing money in production.
        "Status": "Up 3 hours" if state == "running" else "Exited (137) 3 seconds ago",
        "Labels": labels,
        "Ports": [
            {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"},
            {"PrivatePort": 443, "Type": "tcp"},
        ],
        "Mounts": mounts,
        "NetworkSettings": {
            "Networks": {
                "shop_default": {
                    "NetworkID": network_id,
                    "IPAddress": "172.24.0.5",
                    "Aliases": ["web"],
                }
            }
        },
    }


#: `Labels: null`, not `Labels: {}` -- the second trap in these fixtures.
#:
#: A real daemon sends JSON `null` for an empty collection in a dozen ordinary
#: places: `Labels` on a volume, `Aliases` on an endpoint, `IPAM.Config` on a
#: network, `RepoTags` on an untagged image. Nothing announces it, and a
#: deserializer that treats an absent field as a default while treating a null
#: one as a parse error rejects the *entire* List over it -- which is a host
#: that never syncs, not a field that goes missing.
#:
#: `{}` here is what every one of these fixtures said before a real daemon was
#: ever pointed at the agent, and it is why fourteen and twenty-seven checks
#: passed against an agent that could not read a real Docker socket at all.
#: One null anywhere in a payload is enough to hold the whole class, because
#: one null anywhere is enough to lose the whole List.
NULL_LABELS: Any = None

DEFAULT_NETWORK: dict[str, Any] = {
    "Id": "net1", "Name": "shop_default", "Driver": "bridge", "Scope": "local",
    "Internal": False, "Attachable": False, "Ingress": False, "Labels": NULL_LABELS,
    "IPAM": {"Config": [{"Subnet": "172.24.0.0/16"}]},
}

DEFAULT_VOLUME: dict[str, Any] = {
    "Name": "shop_data", "Driver": "local",
    "Mountpoint": "/var/lib/docker/volumes/shop_data/_data",
    "Scope": "local", "CreatedAt": "2026-01-01T00:00:00Z", "Labels": NULL_LABELS,
}

DEFAULT_IMAGE: dict[str, Any] = {
    "Id": "sha256:" + "a" * 64, "RepoTags": ["nginx:latest"],
    "RepoDigests": ["nginx@sha256:" + "b" * 64], "Size": 142_000_000,
    "Created": 1_699_000_000, "Labels": NULL_LABELS,
}
