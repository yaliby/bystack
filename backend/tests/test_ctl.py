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
    base = {
        "url": None,
        "config": None,
        "json": False,
        "ttl": 15,
        "manual": False,
        "version": None,
        "watch": False,
        "status": False,
        "cancel": False,
        "hosts": [],
        "user": "root",
        "key": None,
    }
    return argparse.Namespace(**{**base, **overrides})


def responder(routes: dict[str, Any]):
    """Stand in for `call`, with a Controller that distributes nothing.

    `/agents/releases` is defaulted rather than added to every test below,
    because "this Controller holds no signed release" is the state every one of
    those tests was written against and the one every Controller starts in.
    The tests that care override it.
    """
    answers = {"/agents/releases": NO_RELEASES, **routes}

    def call(url: str, path: str, *, method: str = "GET", body: Any = None) -> Any:
        return answers[path]

    return call


NO_RELEASES: dict[str, Any] = {
    "directory": "/var/lib/bystack/releases",
    "releases": [],
    "upgradable": [],
    "stale": [],
}


HEALTH = {
    "status": "ok",
    "version": "0.2.0",
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
    "upgrade": (
        "curl -fsSL https://raw.githubusercontent.com/yaliby/bystack/v0.2.0"
        "/scripts/install-agent.sh | sudo sh -s -- --controller wss://10.0.0.5:8443"
    ),
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
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/agents": rows, "/healthz": HEALTH, "/agents/enrollment": TERMS}),
    )

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
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/agents": rows, "/healthz": HEALTH, "/agents/enrollment": TERMS}),
    )

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
    rows = [agent("e1", local=True)]
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/agents": rows, "/healthz": HEALTH, "/agents/enrollment": TERMS}),
    )

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
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/agents": rows, "/healthz": HEALTH, "/agents/enrollment": TERMS}),
    )

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


def test_hosts_behind_the_controller_are_named_with_the_way_to_fix_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The agent version was on this route with nothing to compare it against,
    which made it a fact rather than an answer (ADR-0015).

    A report and not a warning: a mixed-version fleet is a normal operating
    state under ADR-0008. What it must not be is invisible.
    """
    rows = [
        agent("current", agent_version="0.2.0"),
        agent("old", agent_version="0.1.0"),
        agent("unseen", agent_version="", connected=False),
    ]
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/agents": rows, "/healthz": HEALTH, "/agents/enrollment": TERMS}),
    )

    ctl.cmd_hosts(args(), "http://x")

    out = capsys.readouterr().out
    assert "1 host(s) behind the Controller (0.2.0)" in out
    assert "install-agent.sh" in out
    # A host nobody has heard from is unknown, not behind. Listing every
    # powered-off machine as something to go and fix is how a report becomes
    # noise.
    assert "0.1.0*" in out
    assert "0.2.0*" not in out


# --------------------------------------------------------------------------
# The rollout (ADR-0017)
# --------------------------------------------------------------------------


HELD = {
    "directory": "/var/lib/bystack/releases",
    "releases": [
        {
            "version": "0.4.0",
            "arch": "x86_64",
            "sha256": "ab" * 32,
            "released_at": 1755388800,
            "size": 2_300_000,
        }
    ],
    "upgradable": ["e1"],
    "stale": ["e2"],
}


def test_a_controller_holding_a_release_offers_the_button_and_not_the_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two answers, and printing both would print one wrong one every time.

    A Controller holding a signed release pushes it down connections it already
    has (ADR-0017); one holding nothing cannot, and those hosts are upgraded by
    the installer (ADR-0015). Which applies is a fact about this Controller.
    """
    rows = [agent("e1", agent_version="0.1.0"), agent("e2", agent_version="0.1.0")]
    monkeypatch.setattr(
        ctl,
        "call",
        responder(
            {
                "/agents": rows,
                "/healthz": HEALTH,
                "/agents/enrollment": TERMS,
                "/agents/releases": HELD,
            }
        ),
    )

    ctl.cmd_hosts(args(), "http://x")

    out = capsys.readouterr().out
    assert "bystack-ctl upgrade" in out
    # The host that cannot take a push is named with the command that fixes it,
    # rather than left out of a run that will silently step over it.
    assert "1 cannot" in out
    assert "install-agent.sh" in out


def test_with_nothing_to_push_the_installer_line_is_still_the_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [agent("e1", agent_version="0.1.0")]
    monkeypatch.setattr(
        ctl,
        "call",
        responder({"/agents": rows, "/healthz": HEALTH, "/agents/enrollment": TERMS}),
    )

    ctl.cmd_hosts(args(), "http://x")

    out = capsys.readouterr().out
    assert "install-agent.sh" in out
    assert "bystack-ctl upgrade" not in out
    assert "No token" in out


def test_a_rollout_that_stopped_on_a_host_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A script that ran this must not go on to report a fleet upgrade that did
    not happen. The exit code is the only part of that a script reads."""
    failed = {
        "version": "0.4.0",
        "state": "failed",
        "planned": ["e1", "e2"],
        "current": None,
        "detail": "e2: it staged 0.4.0 but has not come back on it within 120s.",
        "started_at": 0.0,
        "finished_at": 0.0,
        "results": [
            {"engine_id": "e1", "state": "confirmed", "reason": None, "version": "0.4.0"},
            {"engine_id": "e2", "state": "failed", "reason": "no answer", "version": ""},
        ],
    }
    monkeypatch.setattr(ctl, "call", responder({"/agents/upgrades": failed}))

    assert ctl.cmd_upgrade(args(status=True), "http://x") == 1
    out = capsys.readouterr().out
    assert "1 of 2 confirmed" in out
    assert "has not come back" in out


def test_asking_about_a_rollout_that_never_ran_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ctl, "call", responder({"/agents/upgrades": None}))

    assert ctl.cmd_upgrade(args(status=True), "http://x") == 0
    assert "no rollout" in capsys.readouterr().out


def test_releases_names_the_directory_even_when_it_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The empty answer is the common one, and an empty list with no path is a
    feature that looks broken rather than one with nothing in it yet."""
    monkeypatch.setattr(ctl, "call", responder({}))

    ctl.cmd_releases(args(), "http://x")

    out = capsys.readouterr().out
    assert "/var/lib/bystack/releases" in out
    assert "sign-agent.py" in out


# --------------------------------------------------------------------------
# Deploying agents (ADR-0019)
# --------------------------------------------------------------------------


def test_there_is_no_password_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """An argv password is in the shell history and in `ps` for the whole box.

    Asserted against the parser rather than trusted to a comment: adding
    `--password` would be one convenient-looking line, and this is the only
    thing that would notice.
    """
    parser = ctl.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["deploy", "10.0.0.5", "--password", "hunter2"])

    parsed = parser.parse_args(["deploy", "10.0.0.5", "--user", "root"])
    assert not any("password" in name for name in vars(parsed))


def test_a_deployment_with_no_credential_says_where_to_put_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """And exits 2, so a script does not carry on as though hosts were added."""
    monkeypatch.delenv("BYSTACK_SSH_PASSWORD", raising=False)
    monkeypatch.setattr(ctl.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ctl.sys.stdin, "read", lambda: "")

    assert ctl.cmd_deploy(args(hosts=["10.0.0.5"]), "http://x") == 2
    assert "no --password flag" in capsys.readouterr().err.replace("There is ", "no ")


def test_the_password_is_taken_from_the_environment_and_sent_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The body is the only place it goes, and the output never repeats it."""
    sent: list[Any] = []
    finished = {
        "running": False,
        "started_at": 0,
        "finished_at": 1,
        "error": "",
        "hosts": [
            {
                "host": "10.0.0.5",
                "port": 22,
                "phase": "done",
                "detail": "Connected.",
                "engine_id": "e1",
                "fingerprint": "SHA256:x",
            }
        ],
    }

    def call(url: str, path: str, *, method: str = "GET", body: Any = None) -> Any:
        if body is not None:
            sent.append(body)
        return finished

    monkeypatch.setenv("BYSTACK_SSH_PASSWORD", "hunter2")
    monkeypatch.setattr(ctl, "call", call)

    assert ctl.cmd_deploy(args(hosts=["10.0.0.5"]), "http://x") == 0
    assert sent and sent[0]["password"] == "hunter2"
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "10.0.0.5" in out and "done" in out


def test_a_deployment_that_stopped_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same rule as the rollout: a script must not report a fleet that is not
    there. And the untried hosts are named as untried, not as failures."""
    stopped = {
        "running": False,
        "started_at": 0,
        "finished_at": 1,
        "error": "10.0.0.6: no route to host",
        "hosts": [
            {
                "host": "10.0.0.6",
                "port": 22,
                "phase": "failed",
                "detail": "no route to host",
                "engine_id": "",
                "fingerprint": "",
            },
            {
                "host": "10.0.0.7",
                "port": 22,
                "phase": "waiting",
                "detail": "",
                "engine_id": "",
                "fingerprint": "",
            },
        ],
    }
    monkeypatch.setattr(ctl, "call", responder({"/agents/deploy": stopped}))

    assert ctl.cmd_deploy(args(status=True), "http://x") == 1
    out = capsys.readouterr().out
    assert "were not attempted" in out
    assert "waiting" in out


def test_asking_about_a_deployment_that_never_ran_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ctl, "call", responder({"/agents/deploy": None}))

    assert ctl.cmd_deploy(args(status=True), "http://x") == 0
    assert "no deployment" in capsys.readouterr().out
