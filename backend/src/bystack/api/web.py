"""The dashboard, served by the Controller that owns the data behind it.

Until packaging, the UI existed only under `npm run dev` on port 5173, talking
across an origin to an API on 8000 — which is why `api.cors_origins` defaults
to a vite address. That is a development arrangement, and shipping it would
mean telling an operator to install Node on a control-plane host to look at
their own topology.

**One process, one port, one origin.** The Controller serves the built bundle
itself. Nothing about the API moves: every route stays where it was, this is
mounted underneath them, and an installation that would rather put the bundle
behind nginx can simply not have one here (`BYSTACK_WEB_ROOT` empty, or a
build that never ran) and lose nothing but the convenience.

The search order is deliberately the same shape as the agent binary's
(`runtime/localagent.py`): an explicit override, then the packaged copy, then
the build directory of a source checkout. A contributor who has run
`npm run build` gets the same first run as someone who installed a wheel.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

log = logging.getLogger(__name__)

#: Where a packaged Controller keeps the dashboard it ships with.
#:
#: Absent in a source checkout, which is why the search falls through. Filling
#: it is the wheel build's job and nothing here changes when it does.
BUNDLED: Final = Path(__file__).resolve().parent.parent / "_web"

#: A development checkout: `backend/src/bystack/api/` -> `frontend/dist`.
IN_TREE: Final = Path(__file__).resolve().parents[4] / "frontend" / "dist"

#: What vite names the directory it puts content-hashed files in.
#:
#: Everything under it carries a hash of its own contents in its filename, so
#: it can be cached for as long as a browser is willing to. Everything outside
#: it cannot, and `index.html` least of all — it is the file that names which
#: hashed bundle is current, so a cached copy pins a browser to the previous
#: deployment until it expires.
HASHED: Final = "assets"

#: A year, which is the conventional spelling of "forever" in this header.
IMMUTABLE: Final = "public, max-age=31536000, immutable"

_MISSING = """\
The ByStack dashboard is not installed with this Controller.

The API is running and unaffected -- this is the browser UI only. Either:

  * build it:  cd frontend && npm install && npm run build
  * or point BYSTACK_WEB_ROOT at a directory holding a built index.html
  * or run the dev server: cd frontend && npm run dev

Looked in: {looked}
"""


def web_root() -> tuple[Path | None, str]:
    """Find the built bundle, and say where we looked when there is none.

    An explicitly configured root is an assertion and is never fallen back
    from, for the same reason `local_agent.binary` is not: an operator who
    named a directory and silently got a different dashboard would have no way
    to discover it.
    """
    override = os.environ.get("BYSTACK_WEB_ROOT")
    if override:
        path = Path(override).expanduser()
        if not (path / "index.html").is_file():
            return None, f"BYSTACK_WEB_ROOT is set to {path}, which holds no index.html"
        return path, ""

    candidates = [BUNDLED, IN_TREE]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate, ""
    return None, _MISSING.format(looked=", ".join(str(c) for c in candidates))


class SinglePageApp(StaticFiles):
    """Static files, plus the two rules a single-page app needs.

    **A path that is not a file is the app's, not a 404.** The router lives in
    the browser, so `/hosts` is a real address that no file corresponds to.

    **Except under `assets/`, and except anything the API owns.** Both
    exceptions exist because the failure they prevent is silent: an asset that
    404s into `index.html` arrives at a `<script>` tag as HTML and fails with a
    syntax error pointing at line 1 of a page, and an unknown API path that
    answers 200 with HTML turns a typo in a fetch into a JSON parse error
    instead of the 404 it is.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # A miss is an exception here, not a 404 response -- so catching
            # it is the only way to see one. `html=True` handles a directory
            # and a `404.html`; neither is what a client-side router needs.
            if exc.status_code != 404 or path.startswith(("api/", f"{HASHED}/")):
                raise
        return await super().get_response("index.html", scope)

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Judged on where the file *is* rather than on the request path, so a
        # hashed asset reached through the fallback above cannot be cached
        # under the wrong rule.
        if HASHED in Path(full_path).parts:
            response.headers["cache-control"] = IMMUTABLE
        else:
            # Revalidate, do not refuse to store. `no-cache` still lets the
            # ETag StaticFiles already sets turn the check into a 304, so the
            # cost of being current is a conditional request rather than the
            # whole file.
            response.headers["cache-control"] = "no-cache"
        return response


def mount_web(app: FastAPI) -> Path | None:
    """Serve the dashboard under everything the API already claimed.

    Mounted last, which is the whole of the interaction between the two:
    Starlette matches routes in registration order, so every API route is
    tried first and this only sees what none of them wanted.

    When there is no bundle the mount is replaced by one route that explains
    what is missing, on the reasoning `runtime/localagent.py` uses for a
    missing Docker socket: an operator staring at a blank page needs the
    reason, and "404" is not one.
    """
    root, reason = web_root()
    if root is None:
        log.warning("dashboard not served: %s", reason.splitlines()[0])

        @app.get("/{path:path}", include_in_schema=False)
        async def _no_dashboard(path: str) -> Response:
            # The same exemption the served case makes, for the same reason:
            # an unknown API path must 404 as JSON, or a typo in a fetch
            # surfaces as a parse error instead of a missing route.
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return PlainTextResponse(reason, status_code=404)

        return None

    app.mount("/", SinglePageApp(directory=root, html=True), name="web")
    log.info("dashboard served from %s", root)
    return root
