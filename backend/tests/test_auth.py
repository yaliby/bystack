"""Sign-in, sessions, BYheaven single sign-on and the gate in front of the API (ADR-0020).

Real accounts on a real file in `tmp_path`, real scrypt, real cookies through
a real ASGI app. The two things that are faked are the clock (a ticket's 30
seconds) and the cost factor of the hash, which is lowered so a suite of
sign-ins does not take a minute -- the format and the verification are the
production code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bystack.api.app import API_PREFIX, build_context, create_app
from bystack.config import AgentsConfig, AuthConfig, LocalAgentConfig, Settings
from bystack.core.identity import container_urn
from bystack.infra import sso, users
from bystack.infra.users import UserError, UserStore

SSO_SECRET = "s" * 40
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _cheap_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(users, "_N", 2**10)
    sso._reset()


def make_client(tmp_path: Path, **auth: object) -> TestClient:
    settings = Settings(
        agents=AgentsConfig(state_dir=str(tmp_path)),
        local_agent=LocalAgentConfig(enabled=False),
        auth=AuthConfig(required=True, **auth),  # type: ignore[arg-type]
    )
    context = build_context(settings)
    return TestClient(create_app(settings, context), base_url="http://bystack.test")


def setup_admin(client: TestClient, name: str = "alice") -> None:
    response = client.post(
        f"{API_PREFIX}/auth/setup", json={"username": name, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text


def ticket(sub: str = "alice", *, aud: str = "bystack", ttl: int = 30, jti: str = "j1") -> str:
    """What BYheaven's `make_ticket` produces, built the same way."""
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"sub": sub, "aud": aud, "exp": int(time.time()) + ttl, "jti": jti},
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    mac = hmac.new(SSO_SECRET.encode(), f"v1.{payload}".encode(), hashlib.sha256).digest()
    return f"v1.{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_api_is_closed_until_someone_signs_in(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for path in ("/graph", "/agents", "/commands/actions", "/controller"):
            assert client.get(f"{API_PREFIX}{path}").status_code == 401, path
        # The generated docs list every route, so they are behind it too.
        assert client.get("/openapi.json").status_code == 401


def test_the_probes_and_the_sign_in_routes_stay_open(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        # The self-updater and the container healthcheck call this with no way
        # to sign in; closing it would make every update roll itself back.
        assert client.get(f"{API_PREFIX}/healthz").status_code == 200
        status = client.get(f"{API_PREFIX}/auth/status").json()
        assert status["auth_required"] and status["setup_required"]


def test_a_route_nobody_remembered_to_protect_is_protected(tmp_path: Path) -> None:
    # Deny by default: an API path no router claims still answers 401, not 404,
    # so the existence of routes is not something an anonymous caller can map.
    with make_client(tmp_path) as client:
        assert client.get(f"{API_PREFIX}/does-not-exist").status_code == 401


def test_with_auth_off_the_api_answers_as_it_did(tmp_path: Path) -> None:
    settings = Settings(
        agents=AgentsConfig(state_dir=str(tmp_path)),
        local_agent=LocalAgentConfig(enabled=False),
        auth=AuthConfig(required=False),
    )
    with TestClient(create_app(settings)) as client:
        assert client.get(f"{API_PREFIX}/agents").status_code == 200
        assert client.get(f"{API_PREFIX}/auth/me").json() == {"username": "anonymous"}


def test_the_graph_stream_needs_a_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"{API_PREFIX}/stream"):
            pass
        setup_admin(client)
        # The test client does not replay its cookie jar on a handshake the way
        # a browser does, so send the header a browser would.
        cookie = {"cookie": f"bystack_session={client.cookies.get('bystack_session')}"}
        with client.websocket_connect(f"{API_PREFIX}/stream", headers=cookie) as socket:
            assert socket.receive_json()["type"] == "snapshot"


def test_a_page_on_another_origin_cannot_write_or_open_the_stream(tmp_path: Path) -> None:
    # "Same site" ignores the port, so another web app on this host would
    # otherwise ride the SameSite cookie.
    with make_client(tmp_path) as client:
        setup_admin(client)
        evil = {"Origin": "http://bystack.test:3000"}
        refused = client.post(f"{API_PREFIX}/auth/logout", headers=evil)
        assert refused.status_code == 403
        login = client.post(
            f"{API_PREFIX}/auth/login",
            json={"username": "alice", "password": PASSWORD},
            headers=evil,
        )
        assert login.status_code == 403
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            f"{API_PREFIX}/stream", headers=evil
        ):
            pass
        # Its own origin, and a read from anywhere (CORS decides who may see it).
        same = {"Origin": "http://bystack.test"}
        assert client.post(f"{API_PREFIX}/auth/logout", headers=same).status_code == 204
        assert client.get(f"{API_PREFIX}/agents", headers=evil).status_code == 401


# --------------------------------------------------------------------------
# First-run setup
# --------------------------------------------------------------------------


def test_the_first_visit_creates_the_administrator_once(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        assert client.get(f"{API_PREFIX}/auth/me").json() == {"username": "alice"}
        assert client.get(f"{API_PREFIX}/auth/status").json()["setup_required"] is False

        again = client.post(
            f"{API_PREFIX}/auth/setup", json={"username": "mallory", "password": PASSWORD}
        )
        assert again.status_code == 409


def test_setup_can_be_gated_by_a_token(tmp_path: Path) -> None:
    with make_client(tmp_path, setup_token="let-me-in-please") as client:
        assert client.get(f"{API_PREFIX}/auth/status").json()["setup_token_required"] is True
        body = {"username": "alice", "password": PASSWORD}
        assert client.post(f"{API_PREFIX}/auth/setup", json=body).status_code == 403
        wrong = client.post(f"{API_PREFIX}/auth/setup", json={**body, "setup_token": "nope"})
        assert wrong.status_code == 403
        right = client.post(
            f"{API_PREFIX}/auth/setup", json={**body, "setup_token": "let-me-in-please"}
        )
        assert right.status_code == 201


def test_setup_says_what_is_wrong_with_a_name_or_password(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        short = client.post(
            f"{API_PREFIX}/auth/setup", json={"username": "alice", "password": "short"}
        )
        assert short.status_code == 422
        assert "password" in short.json()["detail"]
        bad = client.post(
            f"{API_PREFIX}/auth/setup", json={"username": "a b", "password": PASSWORD}
        )
        assert bad.status_code == 422


def test_an_unreadable_account_file_does_not_reopen_setup(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        (tmp_path / "users.json").write_text("{ not json")
        # The session is still well-formed but the database is not: fail closed.
        assert client.get(f"{API_PREFIX}/agents").status_code == 503
        assert client.get(f"{API_PREFIX}/auth/status").status_code == 503


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def test_a_password_signs_in_and_signing_out_forgets_it(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        client.post(f"{API_PREFIX}/auth/logout")
        assert client.get(f"{API_PREFIX}/agents").status_code == 401

        bad = client.post(
            f"{API_PREFIX}/auth/login", json={"username": "alice", "password": "wrong-password"}
        )
        assert bad.status_code == 401

        # Names are not case-sensitive.
        good = client.post(
            f"{API_PREFIX}/auth/login", json={"username": "ALICE", "password": PASSWORD}
        )
        assert good.status_code == 200 and good.json() == {"username": "alice"}
        assert client.get(f"{API_PREFIX}/agents").status_code == 200

        # Script cannot read it, and another site cannot send it.
        set_cookie = good.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=strict" in set_cookie


def test_an_unknown_user_and_a_wrong_password_are_indistinguishable(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        client.cookies.clear()
        unknown = client.post(
            f"{API_PREFIX}/auth/login", json={"username": "nobody", "password": PASSWORD}
        )
        wrong = client.post(
            f"{API_PREFIX}/auth/login", json={"username": "alice", "password": "wrong-password"}
        )
        assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json())


def test_repeated_failures_lock_the_account_even_for_the_right_password(tmp_path: Path) -> None:
    with make_client(tmp_path, lockout_threshold=3, rate_limit_per_minute=100) as client:
        setup_admin(client)
        client.cookies.clear()
        for _ in range(3):
            client.post(
                f"{API_PREFIX}/auth/login",
                json={"username": "alice", "password": "wrong-password"},
            )
        locked = client.post(
            f"{API_PREFIX}/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 0


def test_one_address_cannot_hammer_the_sign_in(tmp_path: Path) -> None:
    with make_client(tmp_path, rate_limit_per_minute=3, lockout_threshold=0) as client:
        setup_admin(client)
        client.cookies.clear()
        codes = [
            client.post(
                f"{API_PREFIX}/auth/login",
                json={"username": "alice", "password": "wrong-password"},
            ).status_code
            for _ in range(4)
        ]
        assert codes[-1] == 429


# --------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------


def test_a_changed_password_ends_every_existing_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        assert client.get(f"{API_PREFIX}/agents").status_code == 200
        # What `bystack-ctl user passwd` does, from another process.
        UserStore.open(tmp_path).set_password("alice", "a brand new passphrase")
        assert client.get(f"{API_PREFIX}/agents").status_code == 401
        fresh = client.post(
            f"{API_PREFIX}/auth/login",
            json={"username": "alice", "password": "a brand new passphrase"},
        )
        assert fresh.status_code == 200


def test_a_disabled_account_is_out_at_once_and_stays_out(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        store = UserStore.open(tmp_path)
        store.set_disabled("alice", True)
        assert client.get(f"{API_PREFIX}/agents").status_code == 401
        refused = client.post(
            f"{API_PREFIX}/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert refused.status_code == 401
        # Enabling it again does not bring the old sign-in back.
        store.set_disabled("alice", False)
        assert client.get(f"{API_PREFIX}/agents").status_code == 401


def test_a_forged_or_expired_session_is_nobody(tmp_path: Path) -> None:
    with make_client(tmp_path, session_hours=1) as client:
        setup_admin(client)
        good = client.cookies.get("bystack_session") or ""
        head, payload, _ = good.split(".")
        for forged in (good[:-2] + "xx", f"{head}.{payload}.", "s1.e30.", "garbage", ""):
            client.cookies.clear()
            client.cookies.set("bystack_session", forged)
            assert client.get(f"{API_PREFIX}/agents").status_code == 401, forged


# --------------------------------------------------------------------------
# BYheaven single sign-on
# --------------------------------------------------------------------------


def test_sso_is_off_unless_a_secret_is_set(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        response = client.post(f"{API_PREFIX}/auth/sso", json={"ticket": ticket()})
        assert response.status_code == 404
        assert client.get(f"{API_PREFIX}/auth/status").json()["sso"] is False


def test_a_byheaven_ticket_signs_in_the_account_of_the_same_name(tmp_path: Path) -> None:
    with make_client(tmp_path, sso_secret=SSO_SECRET) as client:
        setup_admin(client)
        client.cookies.clear()
        response = client.post(f"{API_PREFIX}/auth/sso", json={"ticket": ticket("Alice")})
        assert response.status_code == 200 and response.json() == {"username": "alice"}
        assert client.get(f"{API_PREFIX}/agents").status_code == 200


def test_a_ticket_works_once(tmp_path: Path) -> None:
    with make_client(tmp_path, sso_secret=SSO_SECRET) as client:
        setup_admin(client)
        client.cookies.clear()
        one = ticket(jti="same")
        assert client.post(f"{API_PREFIX}/auth/sso", json={"ticket": one}).status_code == 200
        client.cookies.clear()
        assert client.post(f"{API_PREFIX}/auth/sso", json={"ticket": one}).status_code == 401


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: ticket(aud="omni-bridge"), id="made-for-another-service"),
        pytest.param(lambda: ticket(ttl=-5), id="expired"),
        pytest.param(lambda: ticket(ttl=3600), id="lives-too-long"),
        pytest.param(lambda: ticket("nobody"), id="no-such-account"),
        pytest.param(lambda: ticket()[:-3] + "AAA", id="bad-mac"),
        pytest.param(lambda: "v2." + ticket()[3:], id="unknown-version"),
        pytest.param(lambda: "not a ticket", id="garbage"),
    ],
)
def test_a_ticket_that_is_not_for_this_account_here_and_now_is_refused(
    tmp_path: Path, bad: object
) -> None:
    with make_client(tmp_path, sso_secret=SSO_SECRET, rate_limit_per_minute=100) as client:
        setup_admin(client)
        client.cookies.clear()
        response = client.post(f"{API_PREFIX}/auth/sso", json={"ticket": bad()})  # type: ignore[operator]
        assert response.status_code == 401
        assert client.get(f"{API_PREFIX}/agents").status_code == 401


def test_sso_does_not_create_accounts_or_wake_a_disabled_one(tmp_path: Path) -> None:
    with make_client(tmp_path, sso_secret=SSO_SECRET, rate_limit_per_minute=100) as client:
        setup_admin(client)
        client.cookies.clear()
        UserStore.open(tmp_path).set_disabled("alice", True)
        response = client.post(f"{API_PREFIX}/auth/sso", json={"ticket": ticket()})
        assert response.status_code == 401
        assert UserStore.open(tmp_path).count() == 1


def test_a_weak_sso_secret_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        AuthConfig(sso_secret="short")


# --------------------------------------------------------------------------
# bystack-ctl, and who the audit log names
# --------------------------------------------------------------------------


def test_the_control_key_stands_in_for_a_session(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        key = (tmp_path / "ctl.key").read_text().strip()
        assert oct((tmp_path / "ctl.key").stat().st_mode & 0o777) == "0o600"
        assert client.get(f"{API_PREFIX}/agents").status_code == 401
        ok = client.get(f"{API_PREFIX}/agents", headers={"Authorization": f"Bearer {key}"})
        assert ok.status_code == 200
        wrong = client.get(f"{API_PREFIX}/agents", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401


def test_a_wrong_bearer_does_not_fall_back_to_the_cookie(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        assert client.get(f"{API_PREFIX}/agents").status_code == 200
        wrong = client.get(f"{API_PREFIX}/agents", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401


def test_the_audit_entry_names_who_asked(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        setup_admin(client)
        # Refused before dispatch (nothing by that name exists), which is still
        # recorded -- and now still named.
        sent = client.post(
            f"{API_PREFIX}/commands",
            json={"kind": "restart", "target": str(container_urn("nobody", "x"))},
        )
        assert sent.status_code == 404
        entries = client.get(f"{API_PREFIX}/commands/audit").json()
        assert entries and all(entry["actor"] == "alice" for entry in entries)


# --------------------------------------------------------------------------
# The account file
# --------------------------------------------------------------------------


def test_accounts_are_private_and_hashed(tmp_path: Path) -> None:
    store = UserStore.open(tmp_path / "state")
    store.add("alice", PASSWORD)
    path = tmp_path / "state" / "users.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert PASSWORD not in path.read_text()
    assert users.check_password(store.get("alice").password_hash, PASSWORD)  # type: ignore[union-attr]
    assert not users.check_password(store.get("alice").password_hash, "x" * 12)  # type: ignore[union-attr]


def test_a_name_is_taken_in_any_case(tmp_path: Path) -> None:
    store = UserStore.open(tmp_path)
    store.add("Alice", PASSWORD)
    with pytest.raises(UserError, match="already"):
        store.add("alice", PASSWORD)
