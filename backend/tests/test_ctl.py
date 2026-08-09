"""`bystack-ctl`.

Every command is one request to one route, so what is worth testing is not
"does it call the API" — that is `test_agent_trust.py`'s job and it does it
against the real routes. What is worth testing is everything the CLI decides
*around* the request: where to send it, and whether what comes back is
readable by the person who typed it.

`call` is replaced rather than a server started. A CLI whose tests need a
listening socket is one nobody runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from bystack import ctl


def args(**overrides: Any) -> argparse.Namespace:
    base = {"url": None, "config": None, "json": False, "ttl": 15, "manual": False}
    return argparse.Namespace(**{**base, **overrides})


def responder(routes: dict[str, Any]):
    def call(url: str, path: str, *, method: str = "GET", body: Any = None) -> Any:
        return routes[path]

    return call


HEALTH = {
    "status": "ok",
    "seq": 4,
    "node_count": 7,
    "edge_count": 6,
    "read_only": False,
    "providers": [{"id": "e1", "state": "ready"}],
}
TERMS = {
    "enabled": True,
    "auto_approve": False,
    "listen": "0.0.0.0:8443",
    "local_agent": {"state": "running", "detail": "/usr/local/bin/bystack-agent"},
}


def agent(engine_id: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "engine_id": engine_id,
        "status": "approved",
        "certificate_expires_at": 0,
        "enrolled_at": 0,
        "last_seen": 0,
        "agent_version": "0.1.0",
        "connected": True,
        "local": False,
    }
    return {**base, **overrides}


# --------------------------------------------------------------------------
# Where to talk to
# --------------------------------------------------------------------------


def test_the_flag_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYSTACK_URL", "http://env:1")

    assert ctl.base_url(args(url="http://flag:2")) == "http://flag:2"


def test_the_environment_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYSTACK_URL", "http://env:1")

    assert ctl.base_url(args()) == "http://env:1"


def test_the_config_file_is_read_rather_than_repeated(tmp_path: Path) -> None:
    """An operator on the Controller's host already has a file naming the port.

    Asking them to repeat it in a flag is asking them to keep two copies of one
    fact in step.
    """
    config = tmp_path / "bystack.yaml"
    config.write_text("api:\n  host: 127.0.0.1\n  port: 9001\n")

    assert ctl.base_url(args(config=str(config))) == "http://127.0.0.1:9001"


def test_a_bind_address_of_everywhere_is_not_a_destination(tmp_path: Path) -> None:
    """`0.0.0.0` is where the Controller listens, not somewhere to connect to.

    Dialling it fails in a way that reads like the Controller is down, which is
    the wrong diagnosis to hand someone at the worst possible moment.
    """
    config = tmp_path / "bystack.yaml"
    config.write_text("api:\n  host: 0.0.0.0\n  port: 8000\n")

    assert ctl.base_url(args(config=str(config))) == "http://127.0.0.1:8000"


# --------------------------------------------------------------------------
# What comes back
# --------------------------------------------------------------------------


def test_a_uuid_engine_id_does_not_destroy_the_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Docker 25 made the Engine ID a UUID; before that it was 64 hex digits.

    Any constant column width is wrong for one of them, and a width that is too
    small does not truncate — it shifts every column on that row and leaves the
    rest of the table looking like a different table.
    """
    rows = [agent("f952ee52-b480-42d6-bf22-cae4fddb142c"), agent("AAAABBBBCCCC")]
    monkeypatch.setattr(ctl, "call", responder({"/agents": rows}))

    ctl.cmd_hosts(args(), "http://x")

    lines = capsys.readouterr().out.splitlines()
    starts = {line.index("approved") for line in lines[1:3]}
    assert len(starts) == 1, lines


def test_a_pending_host_is_named_once_with_the_command_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pending is the one state that needs a person. A list that reports it in
    a column and stops has left the reader to work out what to do about it."""
    rows = [agent("e1"), agent("e2", status="pending", connected=False)]
    monkeypatch.setattr(ctl, "call", responder({"/agents": rows}))

    ctl.cmd_hosts(args(), "http://x")

    out = capsys.readouterr().out
    assert "bystack-ctl approve e2" in out
    assert "bystack-ctl approve e1" not in out


def test_no_certificate_is_a_dash_and_never_the_epoch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Controller's own agent never enrolled, so its expiry is zero.

    Rendering that as a date in 1970 is a fabricated fact, and a fleet list is
    exactly where somebody would read it as one.
    """
    monkeypatch.setattr(ctl, "call", responder({"/agents": [agent("e1", local=True)]}))

    ctl.cmd_hosts(args(), "http://x")

    assert "1970" not in capsys.readouterr().out


def test_a_token_is_measured_in_minutes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A join token lives fifteen minutes by design. A calendar date for it is
    a true statement that answers nothing."""
    expires = int(dt.datetime.now(tz=dt.UTC).timestamp()) + 15 * 60
    monkeypatch.setattr(
        ctl,
        "call",
        responder(
            {
                "/agents/tokens": {"install": "curl …", "manual": "bystack-agent …",
                                   "expires_at": expires, "token": "bst1.x.y",
                                   "ca_fingerprint": "ff"},
                "/agents/enrollment": TERMS,
            }
        ),
    )

    ctl.cmd_token(args(), "http://x")

    out = capsys.readouterr().out
    assert "14 minutes" in out or "15 minutes" in out
    assert "1970" not in out


def test_minting_against_a_closed_listener_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Minting succeeds whether or not the listener is bound.

    That gap is why `GET /agents/enrollment` exists for the dashboard, and it
    is worse on a CLI: the output is a command that will fail at the dial, and
    nothing about it says why.
    """
    monkeypatch.setattr(
        ctl,
        "call",
        responder(
            {
                "/agents/tokens": {"install": "curl …", "manual": "bystack-agent …",
                                   "expires_at": 0, "token": "t", "ca_fingerprint": "ff"},
                "/agents/enrollment": {**TERMS, "enabled": False},
            }
        ),
    )

    ctl.cmd_token(args(), "http://x")

    out = capsys.readouterr().out
    assert "agents.enabled is false" in out
    assert "0.0.0.0:8443" in out


def test_a_controller_seeing_nothing_is_distinguished_from_one_that_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/healthz": {**HEALTH, "providers": []}, "/agents/enrollment": TERMS}),
    )

    ctl.cmd_status(args(), "http://x")

    assert "nothing is being observed" in capsys.readouterr().out


def test_json_is_the_whole_payload_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` has to be pipeable, so the table's commentary must not leak
    into it — including the advice about pending hosts."""
    rows = [agent("e2", status="pending", connected=False)]
    monkeypatch.setattr(ctl, "call", responder({"/agents": rows}))

    ctl.cmd_hosts(args(json=True), "http://x")

    assert json.loads(capsys.readouterr().out) == rows


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


def test_an_unreachable_controller_says_what_to_try(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise ctl.Failed("cannot reach a Controller at http://x (Connection refused)")

    monkeypatch.setattr(ctl, "call", refuse)

    assert ctl.main(["--url", "http://x", "status"]) == 1
    assert "cannot reach a Controller" in capsys.readouterr().err


def test_a_missing_config_file_is_a_different_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Two is "you asked for something that is not there", one is "the
    Controller said no". A script that retries on failure should not retry a
    path that will never exist."""
    assert ctl.main(["--config", str(tmp_path / "absent.yaml"), "status"]) == 2
    assert "not found" in capsys.readouterr().err
