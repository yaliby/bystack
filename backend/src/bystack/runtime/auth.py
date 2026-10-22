"""Sign-in, sessions and the credentials that stand in for them (ADR-0020).

Everything about *who is calling* is decided here and nowhere else: the gate
(`api/gate.py`) asks one question of this service, and the routes
(`api/routes/auth.py`) turn its answers into responses. Nothing in this file
knows about HTTP.

What it keeps, and where:

* **Accounts** — `UserStore`, a JSON file in the state directory.
* **A session key** — `<state_dir>/session.key`, 32 random bytes, created on
  first start. Sessions are `s1.<payload>.<mac>` (HMAC-SHA256), carrying the
  account's name and its `token_version`. Every request re-reads the account,
  so disabling it or changing its password ends its sessions on the next
  request, whatever time is left on them.
* **A control key** — `<state_dir>/ctl.key`, what `bystack-ctl` presents as a
  bearer token. Whoever can read the state directory already holds the CA
  private key, so the file is no new door; it is the same one, named.

What it counts, in memory, because a restart forgetting them costs nothing:
failed sign-ins per account, and attempts per client address.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path

from bystack.config import AuthConfig
from bystack.infra.sso import verify_ticket
from bystack.infra.users import SetupDone, User, UserError, UserStore, check_password, dummy_hash

log = logging.getLogger(__name__)

CTL_USER = "bystack-ctl"
"""The name a request authenticated by the control key is audited under."""

_TRACKED = 10_000
"""Most accounts and addresses remembered, so random names cannot grow memory."""


class AuthFailure(Exception):
    """A refusal with the status the route should send."""

    def __init__(self, status: int, detail: str, retry_after: int = 0) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


class AuthService:
    def __init__(
        self, users: UserStore, config: AuthConfig, state_dir: str | os.PathLike[str]
    ) -> None:
        self.users = users
        self.config = config
        base = Path(state_dir).expanduser()
        self._session_key = _secret(base / "session.key").encode()
        self._ctl_key = _secret(base / "ctl.key")
        self._failures: dict[str, deque[float]] = {}
        self._attempts: dict[str, deque[float]] = {}

    # -- who is calling ----------------------------------------------------

    def principal(self, cookie: str | None, bearer: str | None) -> str | None:
        """The name a request acts as, or ``None`` for nobody.

        A bearer that is present and wrong never falls through to the cookie:
        a caller that sent a credential and got it wrong is not anonymous.
        """
        if bearer is not None:
            return CTL_USER if hmac.compare_digest(bearer, self._ctl_key) else None
        user = self._session_user(cookie) if cookie else None
        return user.username if user else None

    def issue_session(self, user: User) -> str:
        expires = int(time.time()) + self.config.session_hours * 3600
        payload = _b64(
            json.dumps({"u": user.key, "tv": user.token_version, "exp": expires}).encode()
        )
        return f"s1.{payload}.{self._mac(payload)}"

    def _session_user(self, token: str) -> User | None:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "s1":
            return None
        _, payload, mac = parts
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            name, version, expires = claims["u"], claims["tv"], claims["exp"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(expires, int) or expires <= time.time():
            return None
        user = self.users.get(str(name))
        if user is None or user.disabled or user.token_version != version:
            return None
        return user

    def _mac(self, payload: str) -> str:
        digest = hmac.new(self._session_key, f"s1.{payload}".encode(), hashlib.sha256).digest()
        return _b64(digest)

    # -- signing in --------------------------------------------------------

    async def login(self, username: str, password: str, ip: str) -> User:
        self._rate(ip)
        key = username.strip().lower()
        wait = self._locked(key)
        if wait:
            log.warning("sign-in for %r refused: locked for %ds (from %s)", key, wait, ip)
            minutes = max(1, -(-wait // 60))
            raise AuthFailure(
                429, f"Too many failed attempts. Try again in {minutes} min.", retry_after=wait
            )

        user = self.users.get(key)
        stored = user.password_hash if user else dummy_hash()
        # Off the event loop: a password check is ~100 ms of CPU, and this loop
        # is the one driving every agent's connection.
        correct = await asyncio.to_thread(check_password, stored, password)
        if user is None or not correct or user.disabled:
            self._record_failure(key)
            reason = "disabled" if user and user.disabled else "bad credentials"
            log.warning("sign-in for %r failed: %s (from %s)", key, reason, ip)
            raise AuthFailure(401, "Invalid credentials")

        self._failures.pop(key, None)
        log.info("%s signed in (from %s)", user.username, ip)
        return user

    def sso(self, ticket: str, ip: str) -> User:
        """A BYheaven ticket naming an existing, enabled account replaces the
        password — BYheaven's own sign-in is the only factor. It creates nothing:
        a BYheaven user with no account here is refused."""
        self._rate(ip)
        if not self.config.sso_secret:
            raise AuthFailure(404, "Single sign-on is not enabled")
        name = verify_ticket(
            ticket, secret=self.config.sso_secret, audience=self.config.sso_audience
        )
        user = self.users.get(name) if name else None
        if user is None or user.disabled:
            what = "no enabled account of that name" if name else "invalid ticket"
            log.warning("single sign-on for %r failed: %s (from %s)", name, what, ip)
            raise AuthFailure(401, "Single sign-on failed")
        log.info("%s signed in through BYheaven (from %s)", user.username, ip)
        return user

    async def setup(self, username: str, password: str, token: str, ip: str) -> User:
        """Create the first account, and only the first."""
        self._rate(ip)
        if self.config.setup_token and not hmac.compare_digest(token, self.config.setup_token):
            log.warning("setup refused: wrong setup token (from %s)", ip)
            raise AuthFailure(403, "Invalid setup token")
        try:
            user = await asyncio.to_thread(self.users.add, username, password, only_first=True)
        except SetupDone as exc:
            raise AuthFailure(409, "Setup already completed") from exc
        except UserError as exc:
            raise AuthFailure(422, str(exc)) from exc
        log.info("first account %r created (from %s)", user.username, ip)
        return user

    # -- throttles ---------------------------------------------------------

    def _rate(self, ip: str) -> None:
        """Per-address limit on every route that tries a secret."""
        now = time.monotonic()
        window = _window(self._attempts, ip, now, 60.0)
        if len(window) >= self.config.rate_limit_per_minute:
            wait = max(1, int(60 - (now - window[0])))
            raise AuthFailure(429, "Too many attempts. Slow down.", retry_after=wait)
        window.append(now)

    def _locked(self, key: str) -> int:
        """Seconds until the account accepts attempts again, or 0.

        Attempts made while locked are not recorded, so an attacker cannot keep
        extending the lock and the real owner is back in at most one window.
        """
        limit = self.config.lockout_threshold
        if limit <= 0:
            return 0
        span = self.config.lockout_minutes * 60.0
        now = time.monotonic()
        window = _window(self._failures, key, now, span)
        if len(window) < limit:
            return 0
        return max(1, int(window[-limit] + span - now))

    def _record_failure(self, key: str) -> None:
        span = self.config.lockout_minutes * 60.0
        now = time.monotonic()
        _window(self._failures, key, now, span).append(now)


def _window(
    table: dict[str, deque[float]], key: str, now: float, span: float
) -> deque[float]:
    """The recent entries for ``key``, with the stale ones dropped."""
    if key not in table and len(table) >= _TRACKED:
        del table[next(iter(table))]
    window = table.setdefault(key, deque())
    while window and now - window[0] >= span:
        window.popleft()
    return window


def _secret(path: Path) -> str:
    """Read a 256-bit secret as hex text, creating it (0600) the first time."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        text = path.read_text().strip()
        if len(text) < 64:
            raise UserError(f"{path} is damaged; delete it to issue a new one") from None
        return text
    text = secrets.token_hex(32)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text + "\n")
    return text


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
