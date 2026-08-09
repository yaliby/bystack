"""The dashboard the Controller serves for itself.

Every test here builds a bundle in `tmp_path` and points `BYSTACK_WEB_ROOT` at
it, so nothing depends on whether `npm run build` has ever run in this
checkout — which is also the state CI is in for the Python job.

What is worth testing is not "static files are served". It is the four places
where serving a single-page app and serving an API interfere with each other,
each of which fails *silently* when it is wrong: HTML where JSON was expected,
HTML where JavaScript was expected, a browser pinned to yesterday's bundle,
and a client route that 404s because no file has its name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bystack.api import web
from bystack.api.app import API_PREFIX, create_app
from bystack.api.web import IMMUTABLE, web_root
from bystack.config import Settings

INDEX = "<!doctype html><title>ByStack</title><script src=/assets/app.abc123.js></script>"
BUNDLE = "console.log('bystack')"


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "app.abc123.js").write_text(BUNDLE)
    monkeypatch.setenv("BYSTACK_WEB_ROOT", str(root))
    return root


@pytest.fixture
def client(bundle: Path):
    with TestClient(create_app(Settings())) as client:
        yield client


def test_the_index_is_served_at_the_root(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.text == INDEX


def test_a_client_route_gets_the_app_rather_than_a_404(client) -> None:
    """The router is in the browser, so `/hosts` names no file and is not a 404."""
    response = client.get("/hosts")

    assert response.status_code == 200
    assert response.text == INDEX


def test_an_unknown_api_path_still_404s_as_json(client) -> None:
    """The catch-all must not swallow the API.

    Serving `index.html` here would turn a typo in a fetch into a JSON parse
    error somewhere else entirely, which is the reverse of what a 404 is for.
    """
    response = client.get(f"{API_PREFIX}/nonexistent")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_a_missing_asset_stays_missing(client) -> None:
    """A 404 under `assets/` must not become HTML.

    It arrives at a `<script>` tag, and the browser reports a syntax error on
    line 1 of a document — which sends whoever is debugging it to the wrong
    file entirely. A deploy that dropped a chunk should look like a deploy
    that dropped a chunk.
    """
    response = client.get("/assets/app.deadbee.js")

    assert response.status_code == 404
    assert "<!doctype html>" not in response.text.lower()


def test_a_real_api_route_is_unaffected_by_the_mount(client) -> None:
    assert client.get(f"{API_PREFIX}/healthz").json()["status"] == "ok"


def test_hashed_assets_are_cached_forever_and_the_index_never_is(client) -> None:
    """The index names which hashed bundle is current.

    Caching it is how a browser stays on the previous deployment until an
    expiry it chose, with no way for the operator to tell.
    """
    assert client.get("/assets/app.abc123.js").headers["cache-control"] == IMMUTABLE
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/hosts").headers["cache-control"] == "no-cache"


def test_a_configured_root_is_never_fallen_back_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who named a directory and silently got a different dashboard
    would have no way to discover it — the rule `local_agent.binary` follows."""
    monkeypatch.setenv("BYSTACK_WEB_ROOT", str(tmp_path / "nowhere"))

    root, reason = web_root()

    assert root is None
    assert "BYSTACK_WEB_ROOT" in reason


def test_without_a_bundle_the_page_says_how_to_build_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank page with no reason is the first-run failure this avoids."""
    # The ordinary shape of the failure: nothing configured, and no build in
    # either place the search looks.
    monkeypatch.delenv("BYSTACK_WEB_ROOT", raising=False)
    monkeypatch.setattr(web, "BUNDLED", tmp_path / "no-wheel")
    monkeypatch.setattr(web, "IN_TREE", tmp_path / "no-checkout")

    with TestClient(create_app(Settings())) as client:
        response = client.get("/")
        assert response.status_code == 404
        assert "npm run build" in response.text

        # And the API is emphatically still up. This is the browser UI only.
        assert client.get(f"{API_PREFIX}/healthz").status_code == 200
        assert client.get(f"{API_PREFIX}/nonexistent").status_code == 404
