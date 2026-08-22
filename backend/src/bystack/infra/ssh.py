"""The one outbound connection the Controller makes to a machine (ADR-0019).

ADR-0008 deleted an agentless design in which the Controller held an SSH key
for every host and reached in over it at will. This is not that, and the
difference is the whole of why it is allowed to exist:

* the credential is a parameter of one request and is never stored,
* the connection is opened to install an agent and closed when it is
  installed,
* afterwards the Controller cannot reach the host at all -- the agent dials
  *out*, which is ADR-0008 unchanged.

## Why a port and an implementation rather than asyncssh calls inline

Not architecture for its own sake. What the deploy service does -- mint a
token, upload a script, run it, read an exit code, decide what a failure means
-- is the part with the decisions in it, and it is the part worth testing
against every way a machine can refuse. Testing that against a real sshd means
a container per case; testing it against a fake session means a dictionary.

So `Session` is four methods, `connect` is the only thing that imports
asyncssh, and `tests/test_deploy.py` never opens a socket.

## Trust on first use, and no prompt

Nobody knows a host's key on a first connection. So the first one records it
and reports the fingerprint, and every later connection to that address
requires the same key or refuses with both fingerprints printed.

There is no "accept?" prompt, deliberately. A prompt is what makes host-key
checking theatre everywhere else -- it arrives at the moment somebody is busy,
it has a default, and the default is yes. What is being installed on the other
end is a root daemon.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

#: How long to wait for a TCP connection and a banner.
#:
#: Short, because the overwhelming majority of failures here are "that address
#: is not a machine" or "sshd is not listening", and an operator who mistyped
#: an octet should be told inside a quarter of a minute rather than after the
#: TCP stack has finished being patient.
CONNECT_TIMEOUT = 20

#: The cap on one command's output that this will hold in memory.
#:
#: The installer prints a few dozen lines. Anything approaching this is a
#: machine that is broken in a way worth truncating rather than reading.
MAX_OUTPUT = 256 * 1024


class SshError(RuntimeError):
    """Something the operator needs a sentence about, not a traceback.

    Every message this carries is shown in a browser, so nothing that reaches
    it may contain a credential. `Credential` has no `__str__` that could leak
    one and is never interpolated into one of these.
    """


@dataclass(frozen=True, slots=True)
class Credential:
    """How to authenticate, for exactly as long as one run takes.

    Frozen and `slots`, with no `__repr__` of its own on purpose: the dataclass
    default would print the secret into any traceback that happened to include
    a frame holding one, and a traceback is the least controlled piece of text
    in the system.

    **Nothing writes this anywhere.** There is no `as_json`, no persistence
    hook, and no route that returns it. That is the whole of what ADR-0019
    promises, and it is a property of there being no code to do it rather than
    of a policy anybody has to remember.
    """

    user: str
    password: str = ""
    private_key: str = ""
    passphrase: str = ""

    def __repr__(self) -> str:  # pragma: no cover - a guard, not a behaviour
        kind = "key" if self.private_key else "password" if self.password else "none"
        return f"Credential(user={self.user!r}, auth={kind})"

    @property
    def valid(self) -> bool:
        return bool(self.user) and bool(self.password or self.private_key)


@dataclass(frozen=True, slots=True)
class Result:
    """What one command did."""

    status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.status == 0

    def failure(self) -> str:
        """The last thing the machine said, for a message an operator reads.

        The *end* of stderr rather than the start: a script that fails prints
        its diagnosis last, and the first eighty characters of a long run are
        reliably the least interesting ones in it.
        """
        text = (self.stderr or self.stdout).strip()
        if not text:
            return f"exit status {self.status}"
        tail = text.splitlines()[-4:]
        return " ".join(line.strip() for line in tail if line.strip())


class Session(Protocol):
    """One open connection. Everything the deploy service is allowed to do."""

    async def run(self, command: str) -> Result: ...

    async def upload(self, data: bytes, remote: str, mode: int = 0o600) -> None: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------
# Known hosts
# --------------------------------------------------------------------------


def fingerprint(key: bytes) -> str:
    """The `SHA256:...` spelling every ssh client prints.

    The same format as `ssh-keygen -l`, deliberately: the operator's way to
    check this is to look at the machine, and a fingerprint they cannot
    compare to the one their terminal shows is one nobody compares.
    """
    digest = hashlib.sha256(key).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


@dataclass
class KnownHosts:
    """What key each address answered with, the first time.

    In the state directory beside the CA, which is already `0700` and already
    the one directory worth backing up. It holds public keys: it is a record of
    what was seen rather than a credential, and losing it costs one
    re-acceptance per host.

    Loaded and written per call rather than cached. This is touched a handful
    of times per deployment, and a cached copy is a copy that disagrees with
    the file after an operator has edited it -- which is the supported way to
    forget a host whose key genuinely changed.
    """

    path: Path
    _seen: dict[str, str] = field(default_factory=dict)

    def load(self) -> dict[str, str]:
        seen: dict[str, str] = {}
        try:
            text = self.path.read_text()
        except OSError:
            return seen
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            address, _, print_ = line.partition(" ")
            if address and print_:
                seen[address] = print_.strip()
        return seen

    def check(self, address: str, key: bytes) -> str | None:
        """`None` if this is what we saw before or the first sighting.

        Returns the sentence to refuse with otherwise. A caller that treats a
        string as "carry on" is a caller that has inverted the check, which is
        why this returns the *message* rather than a bool.
        """
        current = fingerprint(key)
        previous = self.load().get(address)
        if previous is None or previous == current:
            return None
        return (
            f"{address} presented host key {current}, and the last time it was "
            f"{previous}. Nothing has been installed. Either that machine was "
            f"rebuilt -- in which case remove its line from {self.path} -- or "
            f"this is not the machine you think it is."
        )

    def remember(self, address: str, key: bytes) -> str:
        """Record the key if it is new, and return its fingerprint either way."""
        current = fingerprint(key)
        seen = self.load()
        if seen.get(address) == current:
            return current
        seen[address] = current
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(f"{address} {print_}\n" for address, print_ in sorted(seen.items()))
            temporary = self.path.with_name(f".{self.path.name}.new")
            temporary.write_text(
                "# Host keys ByStack has seen, one per address (ADR-0019).\n"
                "# Public keys only -- nothing here is a credential.\n"
                "# Delete a line to accept a rebuilt machine's new key.\n" + body
            )
            temporary.replace(self.path)
        except OSError as exc:
            # Not fatal. The cost of failing to record is that the next
            # deployment to this address accepts a first sighting again, which
            # is where it would have been anyway -- and refusing to install
            # because a note could not be filed would be the tail wagging the
            # dog.
            log.warning("cannot record the host key for %s: %s", address, exc)
        return current


# --------------------------------------------------------------------------
# The implementation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int = 22

    @property
    def address(self) -> str:
        return self.host if self.port == 22 else f"[{self.host}]:{self.port}"


class _AsyncsshSession:
    """`Session` over an open asyncssh connection."""

    __slots__ = ("_connection", "_where")

    def __init__(self, connection: object, where: str) -> None:
        self._connection = connection
        self._where = where

    async def run(self, command: str) -> Result:
        try:
            completed = await self._connection.run(  # type: ignore[attr-defined]
                command, check=False, encoding="utf-8", errors="replace"
            )
        except Exception as exc:  # noqa: BLE001 - asyncssh raises a family
            raise SshError(f"{self._where}: {command.split()[0]} failed: {exc}") from exc
        return Result(
            status=int(completed.exit_status or 0),
            stdout=str(completed.stdout or "")[:MAX_OUTPUT],
            stderr=str(completed.stderr or "")[:MAX_OUTPUT],
        )

    async def upload(self, data: bytes, remote: str, mode: int = 0o600) -> None:
        """Write bytes to a path on the host.

        SFTP where the host has a subsystem for it, which is the ordinary case,
        and `cat > file` where it does not -- some appliances and busybox
        images ship sshd with no SFTP at all, and refusing those would be
        refusing exactly the small machines this product is for.
        """
        try:
            async with self._connection.start_sftp_client() as sftp:  # type: ignore[attr-defined]
                async with sftp.open(remote, "wb") as handle:
                    await handle.write(data)
                await sftp.chmod(remote, mode)
            return
        except Exception as exc:  # noqa: BLE001 - no SFTP is not an error yet
            log.debug("sftp to %s failed (%s); falling back to a pipe", self._where, exc)

        try:
            process = await self._connection.create_process(  # type: ignore[attr-defined]
                f"cat > {remote} && chmod {mode:o} {remote}", encoding=None
            )
            process.stdin.write(data)
            process.stdin.write_eof()
            completed = await process.wait()
        except Exception as exc:  # noqa: BLE001
            raise SshError(f"{self._where}: cannot write {remote}: {exc}") from exc
        if completed.exit_status:
            raise SshError(f"{self._where}: cannot write {remote} (exit {completed.exit_status})")

    async def close(self) -> None:
        try:
            self._connection.close()  # type: ignore[attr-defined]
            await self._connection.wait_closed()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a close that fails is not a failure
            pass


@dataclass(frozen=True, slots=True)
class Opened:
    session: Session
    fingerprint: str


async def connect(
    endpoint: Endpoint, credential: Credential, known_hosts: KnownHosts
) -> Opened:
    """Open one connection, or say in one sentence why not.

    The host key is checked *before* the credential is offered. asyncssh
    validates the key during the handshake, so this asks it not to -- and then
    does the comparison itself against `known_hosts`, refusing before there is
    a session to authenticate on. Handing a root password to a machine and
    *then* noticing its key changed is the wrong order to do those two things
    in.
    """
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - a packaging failure
        raise SshError(
            "this Controller was built without asyncssh, so it cannot install an agent "
            "over SSH. Use the command in the dialog instead -- it needs nothing here."
        ) from exc

    if not credential.valid:
        raise SshError("a user and either a password or a private key are needed")

    client_keys: list[object] = []
    if credential.private_key:
        try:
            client_keys = [
                asyncssh.import_private_key(
                    credential.private_key, passphrase=credential.passphrase or None
                )
            ]
        except Exception as exc:  # noqa: BLE001 - asyncssh raises a family
            raise SshError(
                f"that private key could not be read: {exc}. It wants the whole file, "
                f"beginning with -----BEGIN."
            ) from exc

    try:
        connection = await asyncio.wait_for(
            asyncssh.connect(
                endpoint.host,
                port=endpoint.port,
                username=credential.user,
                password=credential.password or None,
                client_keys=client_keys or None,
                # Checked below, against our own record. See the docstring.
                known_hosts=None,
                # An agent on the Controller's account would be a credential
                # this process did not receive in the request -- which is
                # exactly the thing ADR-0019 says it must not use.
                agent_path=None,
            ),
            timeout=CONNECT_TIMEOUT,
        )
    except TimeoutError as exc:
        raise SshError(
            f"{endpoint.host} did not answer on port {endpoint.port} within "
            f"{CONNECT_TIMEOUT}s. Is sshd listening, and is this machine allowed to "
            f"reach it?"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - asyncssh raises a family
        raise SshError(f"cannot connect to {endpoint.host}: {_reason(exc)}") from exc

    key = connection.get_server_host_key()
    raw = key.public_data if key is not None else b""
    refusal = known_hosts.check(endpoint.address, raw)
    if refusal is not None:
        connection.close()
        raise SshError(refusal)
    seen = known_hosts.remember(endpoint.address, raw)
    return Opened(session=_AsyncsshSession(connection, endpoint.host), fingerprint=seen)


def _reason(exc: BaseException) -> str:
    """An asyncssh failure as a sentence, with the common one named.

    "Permission denied" is what an operator sees for a wrong password, a wrong
    user, and a key the host does not accept -- three different mistakes with
    one message, and the second is the one people actually make.
    """
    text = str(exc) or exc.__class__.__name__
    if "auth" in text.lower() or "permission denied" in text.lower():
        return f"{text}. Check the user name as well as the password -- root is not the default."
    return text
