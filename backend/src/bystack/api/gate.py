"""The one place that decides whether a browser-side request may proceed (ADR-0020).

A pure ASGI middleware rather than a dependency on each router, for two
reasons that are the whole point of it:

* **Deny by default.** Everything under `/api` (and the generated docs) needs
  a session unless it is named in :data:`PUBLIC`. A route added next year is
  protected without anyone remembering to protect it.
* **WebSockets.** `BaseHTTPMiddleware` does not see them, and the graph stream
  is the most sensitive thing this port serves. The browser sends its cookie
  with the handshake, so the same check covers it with no ticket to mint.

It also refuses cross-origin writes. A `SameSite=Strict` cookie is not enough
on its own: "same site" ignores the port, so another web app on this host
(OmniBridge on :3000, say) counts as the same site. A browser always sends
`Origin` on a cross-site write and on a WebSocket handshake, so one that does
not match this host — or an origin listed in `api.cors_origins`, which is the
dev server — is refused before it is authenticated.

The control key (`bystack-ctl`) arrives as `Authorization: Bearer`, which a
browser page cannot attach to a cross-origin request without a CORS preflight
this server does not grant.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from bystack.infra.users import UserError
from bystack.runtime.auth import AuthService

log = logging.getLogger(__name__)

COOKIE = "bystack_session"
USER_KEY = "bystack.user"
"""Where the gate leaves the name a request acts as, in the ASGI scope."""

PROTECTED = ("/api/", "/docs", "/redoc", "/openapi.json")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthGate:
    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: AuthService,
        public: Collection[str],
        trusted_origins: Collection[str] = (),
    ) -> None:
        self.app = app
        self.auth = auth
        self.public = frozenset(public)
        self.trusted = frozenset(trusted_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not scope["path"].startswith(PROTECTED):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if not self._same_origin(scope, headers):
            await _refuse(scope, receive, send, 403, "Cross-origin request refused")
            return
        if scope["path"] in self.public:
            await self.app(scope, receive, send)
            return

        bearer = headers.get("authorization", "")
        presented = bearer[7:].strip() if bearer[:7].lower() == "bearer " else None
        try:
            who = self.auth.principal(HTTPConnection(scope).cookies.get(COOKIE), presented)
        except UserError as exc:
            # Fail closed, and say why in the log: an unreadable user database
            # must not read as "no users", which would reopen first-run setup.
            log.error("refusing requests: %s", exc)
            await _refuse(scope, receive, send, 503, "The account database is unreadable")
            return
        if who is None:
            await _refuse(scope, receive, send, 401, "Sign in required")
            return

        scope[USER_KEY] = who
        await self.app(scope, receive, send)

    def _same_origin(self, scope: Scope, headers: Headers) -> bool:
        origin = headers.get("origin")
        if origin is None:
            return True  # not a browser making a cross-site request
        if scope["type"] == "http" and scope["method"] in SAFE_METHODS:
            return True
        return origin in self.trusted or urlsplit(origin).netloc == headers.get("host")


async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        await receive()  # the connect event; closing before accept answers 403
        await send({"type": "websocket.close", "code": 1008})
        return
    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
