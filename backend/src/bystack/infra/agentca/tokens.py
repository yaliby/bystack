"""Single-use, short-lived join tokens.

A token is a bootstrap credential and nothing else. It grants exactly one
capability -- "obtain one certificate" -- for a few minutes, and it is worth
nothing the moment it is redeemed or expires. A token leaked from a shell
history, a config-management log or a screenshot after it was used is worth
nothing. Compare a long-lived shared secret on every host, which is worth the
whole fleet forever (ADR-0011).

**The token carries the CA's fingerprint.** That is what stops enrollment
being trust-on-first-use: the agent knows what the Controller's CA looks like
before it sends anything, so it can refuse to hand its token to whatever
answered the address. The design is k3s's, for k3s's reason.

    bst1.<ca fingerprint, sha-256 hex>.<secret, base64url>

Tokens live in memory and die with the process, which is the same lifetime
they already had: they are valid for minutes, and a Controller restart
invalidating the two outstanding ones costs an operator one command. Making
them durable would mean a secret at rest, with a retention policy, to save
that. The safe direction is the cheap one here.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

#: 256 bits from the OS. The token is guessable-in-principle for exactly as
#: long as it is valid, and this makes "in principle" the only sense in which
#: that is true.
SECRET_BYTES = 32

PREFIX = "bst1"

#: How many spent tokens we remember, and why we remember any.
#:
#: Purely for the diagnosis. Without it an expired token and a replayed one
#: both come back as "unknown", which reads like a typo -- so the operator
#: retypes a token that was never going to work, and the one case that
#: genuinely is a replay looks like a mistake rather than a signal.
#:
#: Bounded, because an unbounded set of anything an unauthenticated caller can
#: make us allocate is not a set.
_SPENT_REMEMBERED = 256


class Outcome(Enum):
    """Why a token was or was not accepted.

    Separate values rather than a bool, because the operator-facing message
    differs and each one means something different happened: `EXPIRED` is a
    slow operator, `USED` is a replay or a rerun, `UNKNOWN` is a typo or
    another Controller's token.
    """

    REDEEMED = "redeemed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    USED = "already used"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class MintedToken:
    token: str
    expires_at: dt.datetime

    @property
    def expires_at_unix(self) -> int:
        return int(self.expires_at.timestamp())


class JoinTokenStore:
    """Mint and redeem. Nothing else touches a token."""

    __slots__ = ("_fingerprint", "_live", "_spent")

    def __init__(self, ca_fingerprint: str) -> None:
        self._fingerprint = ca_fingerprint
        #: digest -> expiry. Digests rather than secrets, so the plaintext
        #: exists only in the operator's terminal and the agent's argv.
        self._live: dict[str, dt.datetime] = {}
        #: digest -> how it was spent. Sweeping an expired token into here
        #: rather than dropping it is what keeps `EXPIRED` distinguishable
        #: from `UNKNOWN` after any later mint has run.
        self._spent: OrderedDict[str, Outcome] = OrderedDict()

    def mint(self, ttl: dt.timedelta) -> MintedToken:
        secret = secrets.token_bytes(SECRET_BYTES)
        expires_at = _now() + ttl
        self._live[_digest(secret)] = expires_at
        self._sweep()
        encoded = base64.urlsafe_b64encode(secret).decode().rstrip("=")
        return MintedToken(f"{PREFIX}.{self._fingerprint}.{encoded}", expires_at)

    def redeem(self, token: str) -> Outcome:
        """Burn a token, or say why it was not burned.

        Redemption is destructive on the success path and only on the success
        path: an agent whose enrollment fails *after* the token is spent would
        otherwise need a second token to try again, which turns one bad CSR
        into a second trip to the host.
        """
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != PREFIX:
            return Outcome.MALFORMED
        # The fingerprint in the token is the *agent's* check on us, not ours
        # on it -- but a token minted by a different Controller is a mistake
        # worth naming rather than reporting as an unknown secret.
        if not hmac.compare_digest(parts[1], self._fingerprint):
            return Outcome.UNKNOWN
        try:
            secret = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        except (ValueError, TypeError):
            return Outcome.MALFORMED

        digest = _digest(secret)
        expires_at = self._live.get(digest)
        if expires_at is None:
            return self._spent.get(digest, Outcome.UNKNOWN)
        if expires_at <= _now():
            return self._spend(digest, Outcome.EXPIRED)

        self._spend(digest, Outcome.USED)
        return Outcome.REDEEMED

    @property
    def outstanding(self) -> int:
        self._sweep()
        return len(self._live)

    def _spend(self, digest: str, how: Outcome) -> Outcome:
        self._live.pop(digest, None)
        self._spent[digest] = how
        while len(self._spent) > _SPENT_REMEMBERED:
            self._spent.popitem(last=False)
        return how

    def _sweep(self) -> None:
        """Move anything past its expiry out of the live set, not out of memory.

        Forgetting it entirely is what made a token that timed out come back
        as "unknown" -- and which of the two it is decides whether the
        operator mints a new token or goes looking for whoever replayed one.
        """
        now = _now()
        for digest in [d for d, expiry in self._live.items() if expiry <= now]:
            self._spend(digest, Outcome.EXPIRED)


def _digest(secret: bytes) -> str:
    return hashlib.sha256(secret).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
