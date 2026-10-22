"""Accounts on disk (ADR-0020).

One JSON file in the state directory, rewritten whole on every change: a few
accounts edited at human speed, and the thing it must survive is a Controller
killed between two edits. Write-and-rename gives that for one small write —
the same shape as the watch list (`infra/watch/durable.py`), 0600 in a 0700
directory.

**An unreadable file is an error, never "no users".** The first visit to a
Controller with no accounts creates an administrator for whoever asks, so
treating a corrupt `users.json` as empty would hand the Controller to the next
person who loads the page. It raises instead, and the gate answers 503.

Reads are cached by the file's mtime: the gate looks the user up on every
request, and `bystack-ctl user …` edits this file from another process —
a disabled account or a changed password has to take effect on the next
request, not the next restart.

shortcut: no cross-process lock, so `bystack-ctl user` racing a sign-up in the
dashboard can lose one write. Accounts change a few times a year; add a
`flock` here if that ever stops being true.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_FILE = "users.json"

#: scrypt, from the standard library: no dependency for a few lines, and
#: memory-hard where a bare SHA is not. 2**15 x 8 is 32 MiB and ~100 ms.
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 2**26

USERNAME = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
PASSWORD_MIN, PASSWORD_MAX = 8, 128


class UserError(Exception):
    """A request about an account that cannot be honoured, worded for a person."""


class SetupDone(UserError):
    """The first-account form was used after an account already existed."""


@dataclass(frozen=True, slots=True)
class User:
    username: str
    """As typed when the account was created. Identity is the lowercase form."""

    password_hash: str
    disabled: bool = False
    token_version: int = 0
    """Bumped by every change that must end existing sign-ins: a new password,
    disabling. A session carries the value it was issued under, and a mismatch
    is a dead session."""

    created_at: float = 0.0

    @property
    def key(self) -> str:
        return self.username.lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return "$".join(
        ("scrypt", str(_N), str(_R), str(_P), _b64(salt), _b64(digest))
    )


def check_password(stored: str, password: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
        )
        return hmac.compare_digest(digest, _unb64(want))
    except (ValueError, TypeError):
        return False


@functools.cache
def dummy_hash() -> str:
    """Checked against when the name does not exist, so an unknown user costs
    the same time as a wrong password and the response does not say which."""
    return hash_password("bystack-timing-equaliser")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def validate(username: str, password: str | None = None) -> None:
    if not USERNAME.match(username):
        raise UserError(
            "a username is 3-64 characters: letters, digits, dots, hyphens and underscores"
        )
    if password is not None and not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise UserError(f"a password is {PASSWORD_MIN}-{PASSWORD_MAX} characters")


class UserStore:
    __slots__ = ("path", "_lock", "_cache", "_stamp")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cache: dict[str, User] = {}
        self._stamp: tuple[int, int] | None = None

    @classmethod
    def open(cls, directory: str | os.PathLike[str]) -> UserStore:
        base = Path(directory).expanduser()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cls(base / _FILE)

    # -- reads -------------------------------------------------------------

    def _users(self) -> dict[str, User]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._stamp:
            try:
                raw = json.loads(self.path.read_text())
                users = [User(**entry) for entry in raw["users"]]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise UserError(f"cannot read {self.path}: {exc}") from exc
            self._cache = {user.key: user for user in users}
            self._stamp = stamp
        return self._cache

    def count(self) -> int:
        return len(self._users())

    def get(self, username: str) -> User | None:
        return self._users().get(username.lower())

    def all(self) -> list[User]:
        return sorted(self._users().values(), key=lambda user: user.key)

    # -- writes ------------------------------------------------------------

    def add(self, username: str, password: str, *, only_first: bool = False) -> User:
        """Create an account. `only_first` is the dashboard's setup form: it
        must refuse once any account exists, decided under the same lock as the
        write so two simultaneous setups cannot both win."""
        validate(username, password)
        with self._lock:
            users = dict(self._users())
            if only_first and users:
                raise SetupDone("setup is already complete")
            if username.lower() in users:
                raise UserError(f"there is already an account named {username!r}")
            user = User(username, hash_password(password), created_at=time.time())
            users[user.key] = user
            self._save(users)
            return user

    def set_password(self, username: str, password: str) -> User:
        validate(username, password)
        return self._change(username, password_hash=hash_password(password))

    def set_disabled(self, username: str, disabled: bool) -> User:
        return self._change(username, disabled=disabled)

    def _change(self, username: str, **fields: Any) -> User:
        with self._lock:
            users = dict(self._users())
            old = users.get(username.lower())
            if old is None:
                raise UserError(f"no account named {username!r}")
            new = User(**{**asdict(old), **fields, "token_version": old.token_version + 1})
            users[new.key] = new
            self._save(users)
            return new

    def _save(self, users: dict[str, User]) -> None:
        body = json.dumps({"users": [asdict(user) for user in users.values()]}, indent=2)
        temporary = self.path.with_name(f".{_FILE}.{secrets.token_hex(4)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
                if os.geteuid() == 0:
                    # `bystack-ctl user` run as root must not leave a file the
                    # Controller's own account cannot read.
                    owner = self.path.parent.stat()
                    os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._cache, self._stamp = users, None
