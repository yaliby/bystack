"""Installing an agent on a machine the operator named (ADR-0019).

Nothing here opens a socket. `infra/ssh.py` splits the decisions from the
transport precisely so that this file can be a dictionary of canned command
output: what is worth testing is not that asyncssh can connect, it is what the
service does with every way a machine can refuse — an unprivileged account, an
agent that is already there, an installer that exits non-zero, an agent that
installs and never dials back.

The two properties that are not about a happy path get their own tests and are
the reason this feature was allowed to exist at all (ADR-0019):

* **the credential is never stored**, which is asserted against the service's
  own attributes rather than trusted,
* **a changed host key is a refusal**, before the credential is offered.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_controller

import bystack.runtime.deploy as module
from bystack.api.app import API_PREFIX
from bystack.infra.ssh import Credential, KnownHosts, Result, SshError, fingerprint
from bystack.runtime.deploy import (
    DeployError,
    DeployService,
    Phase,
    Sources,
    Target,
    parse_hosts,
)

# --------------------------------------------------------------------------
# A machine, as a dictionary
# --------------------------------------------------------------------------


@dataclass
class FakeSession:
    """One host, described by what its commands print.

    Matched by prefix rather than exactly: the installer command carries a
    minted token that changes every run, and a test that pinned the whole
    string would be asserting the token generator's output.
    """

    answers: dict[str, Result] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)
    uploaded: dict[str, bytes] = field(default_factory=dict)
    closed: bool = False

    async def run(self, command: str) -> Result:
        self.ran.append(command)
        for prefix, answer in self.answers.items():
            if command.startswith(prefix) or prefix in command:
                return answer
        return Result(0, "", "")

    async def upload(self, data: bytes, remote: str, mode: int = 0o600) -> None:
        self.uploaded[remote] = data

    async def close(self) -> None:
        self.closed = True


@dataclass
class Opened:
    session: FakeSession
    fingerprint: str = "SHA256:test"


def root_host(**answers: Result) -> FakeSession:
    """A machine that is root, has no agent, and installs cleanly."""
    canned = {
        "id -u": Result(0, "0\n", ""),
        "uname -m": Result(0, "x86_64\n", ""),
        "systemctl is-active": Result(0, "inactive\n", ""),
    }
    canned.update(answers)
    return FakeSession(answers=canned)


def service(
    tmp_path: Path,
    sessions: dict[str, FakeSession] | FakeSession,
    *,
    enrolled: list[set[str]] | None = None,
    read_only: bool = False,
    with_agent: bool = True,
    fail_connect: str = "",
) -> tuple[DeployService, list[Credential]]:
    """A service wired to fakes, and the credentials it was handed.

    The list is what `test_the_credential_is_not_kept` inspects: it is the only
    place in these tests that a secret exists, so a secret found anywhere else
    came from the code under test.
    """
    installer = tmp_path / "install-agent.sh"
    installer.write_text("#!/bin/sh\nexit 0\n")
    agent = tmp_path / "bystack-agent"
    if with_agent:
        agent.write_bytes(b"ELF-ish")

    seen: list[Credential] = []
    #: Enrolment as a script: one answer per poll. The default is "nothing,
    #: then the new host", which is what a real fleet looks like a second
    #: after an agent starts.
    answers = enrolled if enrolled is not None else [set(), {"engine-new"}]
    state = {"index": 0}

    def enrolled_ids() -> set[str]:
        index = min(state["index"], len(answers) - 1)
        state["index"] += 1
        return answers[index]

    async def connector(endpoint, credential, known_hosts):  # type: ignore[no-untyped-def]
        seen.append(credential)
        if fail_connect:
            raise SshError(fail_connect)
        chosen = sessions if isinstance(sessions, FakeSession) else sessions[endpoint.host]
        return Opened(session=chosen)

    return (
        DeployService(
            sources=Sources(
                installer=installer,
                releases_dir=tmp_path / "releases",
                bundled_agent=agent if with_agent else None,
                bundled_arch="x86_64",
            ),
            known_hosts=KnownHosts(tmp_path / "known_hosts"),
            mint_token=lambda ttl: _Minted("bst1.token"),
            enrolled_ids=enrolled_ids,
            dial_url=lambda: "wss://controller:8443",
            read_only=read_only,
            connector=connector,
        ),
        seen,
    )


@dataclass
class _Minted:
    token: str


async def finish(deploy: DeployService) -> None:
    """Wait for the run's task, however it ended.

    A real sleep rather than `sleep(0)`: the service polls for an enrolment on
    a wall-clock interval, so a loop that only yields never lets that interval
    elapse and the run genuinely has not finished.
    """
    for _ in range(600):
        run = deploy.status()
        if run is not None and not run.running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the run never finished")


@pytest.fixture(autouse=True)
def _brisk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wait like the product does, at a hundredth of the wall clock.

    Patched rather than parameterised: these are constants chosen for a real
    machine on a real link (`runtime/deploy.py`), and threading them through
    the constructor purely so a test can shorten them would put a knob in the
    product that only the suite ever turns.
    """
    monkeypatch.setattr(module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(module, "ENROL_TIMEOUT", 0.3)


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


def test_hosts_are_separated_by_whatever_the_operator_pasted() -> None:
    """Newlines, commas and spaces all separate.

    An operator pasting from an inventory has whichever separator that
    inventory used, and none of them is a mistake worth an error message.
    """
    parsed = parse_hosts("10.0.0.5\n10.0.0.6, 10.0.0.7  db-01.internal\n# a comment\n")
    assert [target.host for target in parsed] == [
        "10.0.0.5",
        "10.0.0.6",
        "10.0.0.7",
        "db-01.internal",
    ]
    assert {target.port for target in parsed} == {22}


def test_a_port_may_be_given_and_ipv6_needs_brackets() -> None:
    """`[::1]:22`, which is the spelling every other tool wants too.

    A bare IPv6 address is full of colons, so guessing which one is a port is
    a guess -- and the failure mode of guessing wrong is connecting to the
    wrong machine rather than an error.
    """
    assert parse_hosts("10.0.0.5:2222") == [Target("10.0.0.5", 2222)]
    assert parse_hosts("[fd00::1]:2222") == [Target("fd00::1", 2222)]
    assert parse_hosts("[fd00::1]") == [Target("fd00::1", 22)]
    with pytest.raises(DeployError):
        parse_hosts("10.0.0.5:not-a-port")
    with pytest.raises(DeployError):
        parse_hosts("[fd00::1")


# --------------------------------------------------------------------------
# A run
# --------------------------------------------------------------------------


async def test_a_host_is_installed_and_waited_for(tmp_path: Path) -> None:
    """The happy path, and what it actually does on the machine."""
    session = root_host()
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert [host.phase for host in run.hosts] == [Phase.DONE]
    assert run.hosts[0].engine_id == "engine-new"

    # The installer and the agent were put there rather than fetched.
    assert "/tmp/bystack-install-agent.sh" in session.uploaded
    assert "/tmp/bystack-agent" in session.uploaded
    installed = [line for line in session.ran if "bystack-install-agent.sh" in line]
    assert installed, "the installer was never run"
    assert "--controller wss://controller:8443" in installed[0]
    assert "--binary /tmp/bystack-agent" in installed[0]
    # And cleaned up, on the way out, whatever happened.
    assert any(line.startswith("rm -f") for line in session.ran)
    assert session.closed


async def test_a_machine_that_already_has_an_agent_is_skipped_not_failed(
    tmp_path: Path,
) -> None:
    """`skipped` is a correct outcome for "make this machine managed".

    Drawn as a failure it would send an operator to look at a host that is
    working, and re-installing over a running agent is how a host that already
    has an identity comes back as a stranger awaiting approval.
    """
    session = root_host(**{"systemctl is-active": Result(0, "active\n", "")})
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert run.hosts[0].phase is Phase.SKIPPED
    assert run.error == "", "a skip is not what stops a run"
    assert "/tmp/bystack-agent" not in session.uploaded


async def test_an_account_that_cannot_become_root_is_refused_before_anything_is_sent(
    tmp_path: Path,
) -> None:
    """Two seconds and a sentence, rather than an installer failing half-way."""
    session = root_host(
        **{"id -u": Result(0, "1000\n", ""), "sudo -n true": Result(0, "\n", "")}
    )
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="deploy", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert run.hosts[0].phase is Phase.FAILED
    assert "sudo" in run.hosts[0].detail
    assert not session.uploaded, "nothing should be uploaded to a host we cannot install on"


async def test_sudo_without_a_password_is_enough(tmp_path: Path) -> None:
    """The ordinary cloud image: an unprivileged account with passwordless sudo."""
    session = root_host(
        **{"id -u": Result(0, "1000\n", ""), "sudo -n true": Result(0, "yes\n", "")}
    )
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="ubuntu", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None and run.hosts[0].phase is Phase.DONE
    installed = [line for line in session.ran if "bystack-install-agent.sh" in line]
    assert installed[0].startswith("sudo -n ")


async def test_an_installed_agent_that_will_not_run_reports_the_agents_own_reason(
    tmp_path: Path,
) -> None:
    """The installer's last words are about the token, and are not the reason.

    When an agent does not come up, `install-agent.sh` exits non-zero after
    saying it has kept the token so a retry can use it. That is true, it is the
    tail of the output, and it is what a naive report would show — while the
    actual cause sits in the unit's journal one command away on a connection
    that is still open.

    The commonest first-install failure is a host with no container engine.
    "cannot reach the docker socket" is a sentence an operator acts on.
    """
    session = root_host(
        **{
            "bystack-install-agent.sh": Result(
                1, "The token is still in /etc/bystack/agent.env so it can keep trying.\n", ""
            ),
            "journalctl": Result(
                0,
                "Aug 22 14:04:46 host bystack-agent[490]: bystack-agent: cannot reach the "
                "docker socket: No such file or directory (os error 2)\n"
                "Aug 22 14:04:46 host systemd[1]: bystack-agent.service: Failed with result "
                "'exit-code'.\n",
                "",
            ),
        }
    )
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert run.hosts[0].phase is Phase.FAILED
    assert "docker socket" in run.hosts[0].detail
    assert "agent.env" not in run.hosts[0].detail, "the token line is not the diagnosis"


async def test_a_host_that_still_has_its_certificate_is_repaired_without_a_token(
    tmp_path: Path,
) -> None:
    """The agent is installed and stopped. That machine is not a new host.

    `install-agent.sh` never touches the certificate directory, so the identity
    survives — and a token beside a stored certificate makes the agent enrol
    again and come back as a stranger awaiting approval while the host you
    actually have goes quiet (ADR-0011, and the trap INSTALL.md documents for
    the pasted upgrade line).

    It also cannot be confirmed by a *new* enrolment, because there will not be
    one. Waiting for one is a run that reports failure two minutes after the
    thing it was asked to do has worked — which is what this test was written
    after watching happen.
    """
    calls = {"n": 0}

    class Repairable(FakeSession):
        async def run(self, command: str) -> Result:
            if command.startswith("systemctl is-active"):
                calls["n"] += 1
                # Down when asked at the start, up once it has been reinstalled.
                return Result(0, "active\n" if calls["n"] > 1 else "failed\n", "")
            if "agent.crt" in command:
                return Result(0, "yes\n", "")
            return await super().run(command)

    session = Repairable(
        answers={"id -u": Result(0, "0\n", ""), "uname -m": Result(0, "x86_64\n", "")}
    )
    # No new engine ever appears, which is the whole point.
    deploy, _ = service(tmp_path, session, enrolled=[{"engine-known"}])
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert run.hosts[0].phase is Phase.DONE, run.hosts[0].detail
    assert "identity it already had" in run.hosts[0].detail

    installed = [line for line in session.ran if "bystack-install-agent.sh" in line]
    assert installed, "the installer never ran"
    assert "--token" not in installed[0], (
        "a token beside a stored certificate re-enrols the host as a stranger"
    )


async def test_a_failed_host_stops_the_run_and_leaves_the_rest_waiting(
    tmp_path: Path,
) -> None:
    """ADR-0017's rule, applied to installs.

    The hosts after the failure are `waiting`, not `failed`: they were not
    attempted, and reporting them as failures would send an operator to check
    machines that nothing has touched.
    """
    broken = root_host(
        **{"bystack-install-agent.sh": Result(1, "", "no space left on device\n")}
    )
    sessions = {"10.0.0.5": broken, "10.0.0.6": root_host(), "10.0.0.7": root_host()}
    deploy, _ = service(tmp_path, sessions)
    deploy.start(
        [Target("10.0.0.5"), Target("10.0.0.6"), Target("10.0.0.7")],
        Credential(user="root", password="pw"),
    )
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert [host.phase for host in run.hosts] == [Phase.FAILED, Phase.WAITING, Phase.WAITING]
    assert "no space left on device" in run.hosts[0].detail
    assert "10.0.0.5" in run.error


async def test_an_agent_that_never_dials_back_is_a_failure_that_says_where_to_look(
    tmp_path: Path,
) -> None:
    """Installed is not connected, and only the second one is the point.

    The message names the address the agent was given, because "the installer
    succeeded and nothing appeared" is almost always the listener being off or
    the dial address being unreachable from that machine.
    """
    deploy, _ = service(tmp_path, root_host(), enrolled=[set()])
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert run.hosts[0].phase is Phase.FAILED
    assert "wss://controller:8443" in run.hosts[0].detail


async def test_a_host_with_no_agent_for_its_architecture_fetches_its_own(
    tmp_path: Path,
) -> None:
    """Not a failure: it is the pasted command's behaviour, and it is said out loud.

    The Controller bundles one architecture. A fleet with both is ordinary,
    and refusing the other half would be worse than telling the operator that
    one step needs egress.
    """
    session = root_host(**{"uname -m": Result(0, "aarch64\n", "")})
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None and run.hosts[0].phase is Phase.DONE
    assert "/tmp/bystack-agent" not in session.uploaded
    installed = [line for line in session.ran if "bystack-install-agent.sh" in line]
    assert "--binary" not in installed[0]


async def test_a_signed_release_is_preferred_over_the_bundled_agent(
    tmp_path: Path,
) -> None:
    """Because the installer can *verify* one, against a key we do not hold.

    An agent placed this way is trusted on the same terms as one pushed by the
    fleet rollout, which is the whole reason `releases_dir` is looked at first.
    """
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "bystack-agent-x86_64").write_bytes(b"signed-elf")
    (releases / "bystack-agent-x86_64.manifest").write_bytes(b"bystack-manifest/1\n")
    (releases / "bystack-agent-x86_64.manifest.sig").write_bytes(b"sig")

    session = root_host()
    deploy, _ = service(tmp_path, session)
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    assert session.uploaded["/tmp/bystack-agent"] == b"signed-elf"
    assert "/tmp/bystack-agent.manifest" in session.uploaded
    assert "/tmp/bystack-agent.manifest.sig" in session.uploaded


# --------------------------------------------------------------------------
# The two properties ADR-0019 rests on
# --------------------------------------------------------------------------


async def test_the_credential_is_not_kept(tmp_path: Path) -> None:
    """The whole of what ADR-0019 promises, asserted rather than trusted.

    A credential that survives a run is a Controller that can reach a host at
    will, which is exactly the arrangement ADR-0008 deleted. This walks the
    service's own attributes looking for the secret, so a future refactor that
    stashes one `for convenience` fails here rather than in a review.
    """
    secret = "hunter2-not-in-any-attribute"
    deploy, seen = service(tmp_path, root_host())
    deploy.start([Target("10.0.0.5")], Credential(user="root", password=secret))
    await finish(deploy)

    assert seen and seen[0].password == secret, "the fake never received it"

    def holds(value: object, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if isinstance(value, str):
            return secret in value
        if isinstance(value, dict):
            return any(holds(item, depth + 1) for item in value.values())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(holds(item, depth + 1) for item in value)
        slots = getattr(type(value), "__slots__", ())
        for name in slots:
            if holds(getattr(value, name, None), depth + 1):
                return True
        return holds(getattr(value, "__dict__", {}), depth + 1) if depth < 4 else False

    assert not holds(deploy), "the service is still holding the password after the run"

    run = deploy.status()
    assert run is not None
    assert not holds(run), "the run record is holding the password"


def test_a_credential_never_reaches_its_own_repr() -> None:
    """A traceback is the least controlled piece of text in the system.

    The dataclass default `__repr__` would print the password into any
    traceback holding a frame with one, and tracebacks reach logs.
    """
    text = repr(Credential(user="root", password="hunter2", private_key="-----BEGIN"))
    assert "hunter2" not in text
    assert "BEGIN" not in text
    assert "root" in text and "key" in text


def test_a_changed_host_key_is_a_refusal_with_both_fingerprints(tmp_path: Path) -> None:
    """No prompt, and the two values printed.

    A prompt is what makes host-key checking theatre everywhere else: it
    arrives when somebody is busy, it has a default, and the default is yes.
    What is on the other end is about to be handed a root password.
    """
    known = KnownHosts(tmp_path / "known_hosts")
    assert known.check("10.0.0.5", b"first-key") is None
    known.remember("10.0.0.5", b"first-key")

    # The same key again is silence.
    assert known.check("10.0.0.5", b"first-key") is None

    refusal = known.check("10.0.0.5", b"second-key")
    assert refusal is not None
    assert fingerprint(b"second-key") in refusal
    assert fingerprint(b"first-key") in refusal
    assert str(known.path) in refusal, "it has to say where to remove the line"


def test_known_hosts_holds_public_keys_and_says_so(tmp_path: Path) -> None:
    """It is a record of what was seen, not a credential.

    Worth a test because the file lives beside the CA key, and a reader who
    assumed everything in that directory was a secret would treat losing it as
    an incident rather than as one re-acceptance per host.
    """
    known = KnownHosts(tmp_path / "known_hosts")
    known.remember("10.0.0.5", b"a-key")
    text = known.path.read_text()
    assert "nothing here is a credential" in text
    assert fingerprint(b"a-key") in text
    assert "a-key" not in text, "the key itself is not written, only its digest"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


async def test_read_only_refuses_to_install_on_anything(tmp_path: Path) -> None:
    """The largest thing this Controller can do to somebody else's computer."""
    deploy, _ = service(tmp_path, root_host(), read_only=True)
    with pytest.raises(DeployError, match="read-only"):
        deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))


async def test_two_runs_at_once_are_refused_with_the_reason(tmp_path: Path) -> None:
    """And the reason is not politeness: sequencing is what makes a new agent
    attributable to the machine that was just installed."""
    deploy, _ = service(tmp_path, root_host(), enrolled=[set()])
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    with pytest.raises(DeployError, match="already running"):
        deploy.start([Target("10.0.0.6")], Credential(user="root", password="pw"))
    await finish(deploy)


async def test_a_credential_with_no_secret_is_refused(tmp_path: Path) -> None:
    deploy, _ = service(tmp_path, root_host())
    with pytest.raises(DeployError, match="password or a private key"):
        deploy.start([Target("10.0.0.5")], Credential(user="root"))


async def test_a_connection_that_fails_is_the_hosts_row_not_a_crash(
    tmp_path: Path,
) -> None:
    deploy, _ = service(tmp_path, root_host(), fail_connect="10.0.0.5 did not answer")
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    run = deploy.status()
    assert run is not None
    assert run.hosts[0].phase is Phase.FAILED
    assert "did not answer" in run.hosts[0].detail


async def test_the_same_address_twice_is_installed_once(tmp_path: Path) -> None:
    """A pasted inventory has duplicates. Installing twice would enrol twice."""
    deploy, _ = service(tmp_path, root_host())
    run = deploy.start(
        [Target("10.0.0.5"), Target("10.0.0.5"), Target("10.0.0.5", 2222)],
        Credential(user="root", password="pw"),
    )
    assert [host.address for host in run.hosts] == ["10.0.0.5", "10.0.0.5:2222"]
    await finish(deploy)


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


def test_the_route_never_answers_with_the_credential(tmp_path: Path) -> None:
    """Write-only in the strict sense: no response model has a field for it."""
    controller = make_controller(tmp_path / "state", read_only=False)
    with TestClient(controller.ui) as client:
        assert client.get(f"{API_PREFIX}/agents/deploy").json() is None

        answer = client.post(
            f"{API_PREFIX}/agents/deploy",
            json={"hosts": "127.0.0.1:1", "user": "root", "password": "hunter2"},
        )
        assert answer.status_code == 200
        assert "hunter2" not in answer.text

        # Port 1 refuses immediately, which is the point: the run has to be
        # allowed to finish, or the task outlives the client's event loop and
        # this test reports a warning instead of an answer.
        for _ in range(200):
            current = client.get(f"{API_PREFIX}/agents/deploy").json()
            if current is not None and not current["running"]:
                break
            time.sleep(0.02)
        assert current is not None and not current["running"]
        assert "hunter2" not in json.dumps(current)


def test_an_unparseable_address_is_a_409_with_a_sentence(tmp_path: Path) -> None:
    """409 rather than 422: it is a state the operator can fix, not a malformed
    request, and the sentence is what tells them how."""
    controller = make_controller(tmp_path / "state", read_only=False)
    with TestClient(controller.ui) as client:
        answer = client.post(
            f"{API_PREFIX}/agents/deploy",
            json={"hosts": "10.0.0.5:pizza", "user": "root", "password": "x"},
        )
    assert answer.status_code == 409
    assert "port" in answer.json()["detail"]


def test_deploying_with_no_credential_is_refused_by_the_route(tmp_path: Path) -> None:
    controller = make_controller(tmp_path / "state", read_only=False)
    with TestClient(controller.ui) as client:
        answer = client.post(
            f"{API_PREFIX}/agents/deploy", json={"hosts": "10.0.0.5", "user": "root"}
        )
    assert answer.status_code == 409
    assert "private key" in answer.json()["detail"]


def test_a_read_only_controller_refuses_the_route(tmp_path: Path) -> None:
    """The platform's safe mode, on the largest thing it can be asked to do."""
    controller = make_controller(tmp_path / "state")
    with TestClient(controller.ui) as client:
        answer = client.post(
            f"{API_PREFIX}/agents/deploy",
            json={"hosts": "10.0.0.5", "user": "root", "password": "x"},
        )
    assert answer.status_code == 409
    assert "read-only" in answer.json()["detail"]


def test_the_dial_url_the_installer_is_given_is_the_one_the_dialog_hands_out(
    tmp_path: Path,
) -> None:
    """Two spellings of this address is how a host added one way reaches the
    listener and a host added the other way does not."""
    controller = make_controller(tmp_path / "state", read_only=False)
    with TestClient(controller.ui) as client:
        terms = client.get(f"{API_PREFIX}/agents/enrollment").json()
        minted = client.post(f"{API_PREFIX}/agents/tokens", json={}).json()
    dial = controller.settings.agents.dial_url
    assert dial in minted["install"]
    assert dial in terms["upgrade"]


def test_the_installer_the_controller_would_upload_is_the_one_in_the_tree(
    tmp_path: Path,
) -> None:
    """The Controller uploads a script rather than making the host fetch one.

    A checkout finds it in `scripts/`; a wheel finds it in `_bundled/`
    (`hatch_build.py`). If neither is there the button cannot work, and this is
    where that is cheap to notice.
    """
    controller = make_controller(tmp_path / "state", read_only=False)
    sources = controller.context.deploy._sources  # noqa: SLF001 - a packaging assertion
    assert sources.installer is not None, "no install-agent.sh to upload"
    assert "install-agent.sh" in sources.installer.name
    assert "--controller" in sources.installer.read_text()


async def test_a_run_that_is_over_stops_the_dashboard_polling(tmp_path: Path) -> None:
    """`running` is what the UI polls on, and it has to become false."""
    deploy, _ = service(tmp_path, root_host())
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)
    run = deploy.status()
    assert run is not None and not run.running and run.finished_at > 0


async def test_the_ttl_the_deployment_mints_is_short(tmp_path: Path) -> None:
    """The token is carried over one SSH connection and redeemed seconds later.

    Asserted because the argument for a token existing at all is that it is
    short-lived (ADR-0011), and a deployment path that quietly minted a
    long-lived one would be the exception nobody reviewed.
    """
    minted: list[dt.timedelta] = []

    def mint(ttl: dt.timedelta) -> _Minted:
        minted.append(ttl)
        return _Minted("bst1.token")

    deploy, _ = service(tmp_path, root_host())
    deploy._mint = mint  # noqa: SLF001 - the seam this assertion needs
    deploy.start([Target("10.0.0.5")], Credential(user="root", password="pw"))
    await finish(deploy)

    assert minted, "no token was minted"
    assert minted[0] <= dt.timedelta(minutes=15)
