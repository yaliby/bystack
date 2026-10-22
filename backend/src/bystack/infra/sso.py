"""BYheaven single-sign-on tickets (ADR-0020).

Format, as BYheaven's `api/server.py::make_ticket` issues it::

    v1.<payload>.<mac>

``payload`` is base64url JSON ``{sub, aud, exp, jti}`` and ``mac`` is
base64url HMAC-SHA256 of ``"v1." + payload`` under a secret shared with
BYheaven. BYheaven makes one per click, for one service, and it lives 30
seconds. The mac proves where it came from, ``aud`` stops a ticket made for
another service being replayed here, and ``jti`` makes it single use.

This is the same check OmniBridge makes (`server/src/services/sso.service.ts`),
so one BYheaven can sign in to both.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

#: A ticket claiming to live longer than this is not one BYheaven made.
MAX_LIFETIME_S = 60

_used: dict[str, int] = {}  # jti -> exp (seconds)


def verify_ticket(
    ticket: str, *, secret: str, audience: str, now: float | None = None
) -> str | None:
    """The BYheaven user a valid, unused ticket names, or ``None``.

    A ticket is burned even when a later check fails, so a replay of one that
    has been seen cannot succeed on a second look.
    """
    if not secret:
        return None
    parts = ticket.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    _, payload, mac = parts
    want = hmac.new(secret.encode(), f"v1.{payload}".encode(), hashlib.sha256).digest()
    try:
        got = base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4))
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(got, want) or not isinstance(claims, dict):
        return None

    moment = int(now if now is not None else time.time())
    sub, aud, exp, jti = (claims.get(key) for key in ("sub", "aud", "exp", "jti"))
    if aud != audience:
        return None
    if not isinstance(exp, int) or isinstance(exp, bool):
        return None
    if exp <= moment or exp - moment > MAX_LIFETIME_S:
        return None
    if not isinstance(jti, str) or not jti:
        return None
    if not isinstance(sub, str) or not sub or len(sub) > 64:
        return None

    for seen, until in list(_used.items()):
        if until <= moment:
            del _used[seen]
    if jti in _used:
        return None
    _used[jti] = exp
    return sub


def _reset() -> None:
    """Test helper."""
    _used.clear()
