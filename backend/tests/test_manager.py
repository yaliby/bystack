"""Updating the Controller itself (ADR-0018).

The Controller's half: writing an intent, reading what root wrote back, and
the one condition under which a successful update rolls the fleet forward
without being asked. The other half -- the download, the signature check, the
version floor, the swap, the probation and the rollback -- is Rust, and is
tested where it runs (`agent/manager/src`), because that is the half whose
correctness is a security property.

Two things are deliberately not tested here, for the same reason they are not
tested in `test_upgrade.py`. There is no test that a bad signature is refused:
this process holds no key and cannot tell, which is the design. And there is
no test that an update *succeeds*, because the middle of a successful update
is this process being stopped -- what can be asserted from here is what it
says before that, and what the next one does when it comes up.

Nothing here runs the manager, touches /opt, or needs root.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_controller

from bystack import __version__
from bystack.api.app import API_PREFIX
from bystack.infra import manager as manager_module
from bystack.infra.manager import ManagerError, ManagerLink
from bystack.runtime.selfupdate import SelfUpdateService

REPO = Path(__file__).resolve().parent.parent.parent


def ipc(tmp_path: Path) -> Path:
    directory = tmp_path / "ipc"
    directory.mkdir()
    return directory


def status_document(**fields: object) -> str:
    body = {"phase": "success", "version": "0.5.0", "cascade": "", "updated_at": "1", "detail": ""}
    body.update({key: str(value) for key, value in fields.items()})
    return manager_module.STATUS_MAGIC + "\n" + "".join(
        f"{key} {value}\n" for key, value in body.items()
    )


# --------------------------------------------------------------------------
# The documents, across the language boundary
# --------------------------------------------------------------------------


def _rust_string(name: str) -> str:
    source = (REPO / "agent" / "manager" / "src" / "ipc.rs").read_text()
    found = re.search(rf'\b{name}\s*:\s*&str\s*=\s*"([^"]*)"', source)
    assert found is not None, f"{name} is no longer defined in agent/manager/src/ipc.rs"
    return found.group(1)


def test_the_ipc_vocabulary_agrees_across_languages() -> None:
    """Two processes, two languages, one text format, nothing generated.

    The same check `test_wire.py` makes for the signed manifest, for the same
    reason and with a worse failure: both ends parse strictly and refuse a
    magic they do not know, so a drift here is a button that writes a file the
    manager rejects, reported to the operator as an update that failed
    instantly for no visible reason.
    """
    assert _rust_string("INTENT_MAGIC") == manager_module.INTENT_MAGIC
    assert _rust_string("STATUS_MAGIC") == manager_module.STATUS_MAGIC

    layout = (REPO / "agent" / "manager" / "src" / "layout.rs").read_text()
    names = (("INTENT", manager_module.INTENT_NAME), ("STATUS", manager_module.STATUS_NAME))
    for name, value in names:
        found = re.search(rf'\b{name}\s*:\s*&str\s*=\s*"([^"]*)"', layout)
        assert found is not None and found.group(1) == value, (
            f"the manager reads {name} from a different file name than the Controller "
            f"writes; the intent would sit there unread"
        )


# --------------------------------------------------------------------------
# The installer, across the file boundary
# --------------------------------------------------------------------------
#
# `scripts/install-controller.sh` is the only thing that ever creates the
# arrangement this whole feature runs on: the account, the directory, the two
# files, the four units and a configuration file naming two of those paths.
# Nothing at runtime checks any of it -- a Controller installed against the
# wrong paths starts perfectly and its update button does nothing, or does half
# of something, which is the failure mode the checks below exist for.
#
# Read as text rather than executed. Running it needs root and a systemd, and
# what is being asserted is not that it works on this machine: it is that the
# constants inside it still name what the rest of the tree names.


def _installer() -> str:
    return (REPO / "scripts" / "install-controller.sh").read_text()


def test_the_installer_puts_the_ipc_directory_where_the_controller_looks() -> None:
    """The one path that has to agree in four places at once.

    The Controller's default, the manager's `Layout`, the `PathExists=` in the
    unit that turns a button into a root action, and the `ReadWritePaths=` that
    decides whether the Controller may write there at all. Any one of them
    moving on its own is a button that writes a file nothing reads -- reported
    to the operator as an update that never started, with nothing in any log
    to say why.
    """
    layout = (REPO / "agent" / "manager" / "src" / "layout.rs").read_text()
    found = re.search(r'DEFAULT_HOME\s*:\s*&str\s*=\s*"([^"]*)"', layout)
    assert found is not None, "the manager no longer has a DEFAULT_HOME to agree with"
    home = found.group(1)

    assert f"{home}/ipc" == manager_module.DEFAULT_IPC_DIR, (
        f"the manager lives in {home} and the Controller writes its intent to "
        f"{manager_module.DEFAULT_IPC_DIR}"
    )

    installer = _installer()
    assert f'HOME_DIR="${{BYSTACK_HOME:-{home}}}"' in installer, (
        f"install-controller.sh installs somewhere other than {home}, which is where "
        f"bystack-manager looks when systemd starts it with no environment"
    )
    # The unit it writes, and the two directives in it that are not style.
    assert "PathExists=${HOME_DIR}/ipc/update.intent" in installer
    assert "ConditionPathExists=${HOME_DIR}/state/probation" in installer
    assert "ReadWritePaths=${HOME_DIR}/ipc ${HOME_DIR}/cache" in installer


def test_the_installer_points_the_controller_at_the_cascades_release_directory() -> None:
    """Phase two lands in a directory the Controller has to be told about.

    `agents.releases_dir` defaults into the *state* directory, and the manager
    drops the fleet's signed agents into the *deployment* directory. They are
    different paths for good reasons -- one is backed up and one is
    re-downloadable -- so the config the installer writes is the only thing
    joining them.

    Get this wrong and the failure is silent and late: the Controller updates
    itself, comes up healthy, reports success, and the fleet is never offered
    the release that was downloaded for it.
    """
    layout = (REPO / "agent" / "manager" / "src" / "layout.rs").read_text()
    assert re.search(r'fn releases\(&self\)[^}]*join\("releases"\)', layout, re.S), (
        "the manager no longer cascades into <home>/releases"
    )
    installer = _installer()
    assert "releases_dir: ${HOME_DIR}/releases" in installer, (
        "the configuration install-controller.sh writes does not point releases_dir at "
        "the directory bystack-manager cascades into; an update would succeed and the "
        "fleet would never be offered it"
    )
    assert "manager_ipc_dir: ${HOME_DIR}/ipc" in installer


def test_the_installer_writes_the_operators_name_as_the_one_that_is_handed_out() -> None:
    """The Controller hands out `server_names[0]`, and only that one.

    `AgentsConfig.dial_url` takes the first name the listener issued itself a
    certificate for, because a wildcard bind is a meaningless thing to dial.
    So the order in the file `install-controller.sh` writes is not cosmetic:
    with the loopback names first, every agent on every *other* machine is told
    to dial `localhost` — itself — and enrolment fails with "Connection
    refused" on a machine where nothing is wrong.

    Found by installing a Controller and deploying to two servers; it is
    invisible on a single-machine install, which is exactly the one every
    developer tries first.
    """
    installer = _installer()
    found = re.search(r"for name in ([^;]+); do", installer)
    assert found is not None, "install-controller.sh no longer builds server_names in a loop"
    order = found.group(1).split()
    assert order[0] == "$server_names", (
        f"the loop writes {order} — the operator's --server-name has to come first, "
        f"or the Controller hands every remote agent a loopback address"
    )


def test_the_installer_pins_the_interpreter_the_release_is_built_for() -> None:
    """The check that pays for the zipapp, one step earlier than the manager's.

    `build-controller.sh` assembles the artifact for one CPython ABI. The
    manager runs `--version` before it stops anything, so a Controller already
    on a host refuses an update it cannot run; the installer has no such
    Controller to fall back to, so it checks before it downloads.
    """
    build = (REPO / "scripts" / "build-controller.sh").read_text()
    found = re.search(r'^PYTHON_VERSION="([^"]+)"', build, re.M)
    assert found is not None, "build-controller.sh no longer names a CPython version"
    assert f"PYTHON_VERSION={found.group(1)}" in _installer(), (
        f"the release is assembled for CPython {found.group(1)} and install-controller.sh "
        f"checks for a different one; a host would install an artifact it cannot start"
    )


def test_the_installer_verifies_both_binaries_by_name() -> None:
    """One key signs three artifacts, so the name is what keeps them apart.

    Both files this installs are root-executed and neither is re-checked by
    anything downstream -- `bystack-manager` least of all, since it is the
    binary holding the key set for every later update. A signed Controller
    installed as the manager would pass the signature, the digest and the
    architecture check.
    """
    installer = _installer()
    for name in ("bystack-controller", "bystack-manager"):
        assert f"obtain {name} " in installer, f"install-controller.sh no longer fetches {name}"
    assert '[ "$signed_name" = "$expect_name" ]' in installer, (
        "the installer no longer checks the manifest's name against the artifact it "
        "thinks it is installing"
    )

    signer = (REPO / "scripts" / "sign-agent.py").read_text()
    assert '"bystack-manager"' in signer, (
        "install-controller.sh verifies a signed bystack-manager and sign-agent.py "
        "cannot produce one; every release of it would fall back to a checksum"
    )
    release = (REPO / "scripts" / "release-agent.sh").read_text()
    assert "ARTIFACTS=(bystack-agent bystack-controller bystack-manager)" in release, (
        "a release run would not sign the manager, so the artifact holding the trust "
        "root would be the one file installed on a checksum"
    )


def test_the_phases_the_dashboard_draws_are_the_phases_the_manager_writes() -> None:
    """A phase this side has never heard of would be drawn as "in progress".

    Which is the wrong way round for `rolled_back`: it is a *terminal* state,
    and treating it as running leaves the progress bar spinning forever over a
    machine that has already finished and gone back to where it was.
    """
    source = (REPO / "agent" / "manager" / "src" / "ipc.rs").read_text()
    written = set(re.findall(r'Phase::\w+ => "(\w+)"', source))
    assert written, "the manager's phase names are no longer written where this can read them"
    assert written >= manager_module.TERMINAL, (
        f"the Controller treats {sorted(manager_module.TERMINAL - written)} as terminal "
        f"and the manager never writes them"
    )


def test_an_unknown_key_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    """Symmetric with the Rust end, and with the manifest parser above it.

    A field added later because it carries a constraint -- a deadline, a
    reason to stop -- must not be quietly ignored by an older reader that goes
    on to act on the rest of the document.
    """
    directory = ipc(tmp_path)
    (directory / manager_module.STATUS_NAME).write_text(
        status_document() + "expires_at 1\n"
    )
    assert ManagerLink(directory).status() is None


def test_a_status_file_that_is_not_one_is_none_rather_than_an_exception(tmp_path: Path) -> None:
    """This is polled from a route a browser is holding open.

    A Controller that started returning 500s because root wrote something
    unexpected would be a worse failure than the one it was trying to report.
    """
    directory = ipc(tmp_path)
    (directory / manager_module.STATUS_NAME).write_text("not a document at all\n")
    assert ManagerLink(directory).status() is None


def test_the_intent_is_written_whole(tmp_path: Path) -> None:
    """The path unit fires on the file existing, so a partial write is a
    trigger for a document that is not finished."""
    directory = ipc(tmp_path)
    link = ManagerLink(directory)
    link.request("0.5.0", "http://127.0.0.1:8000/api/v1/healthz")

    document = (directory / manager_module.INTENT_NAME).read_text()
    assert document.startswith(manager_module.INTENT_MAGIC + "\n")
    assert "version 0.5.0\n" in document
    assert "health http://127.0.0.1:8000/api/v1/healthz\n" in document
    # Nothing left beside it. A `.update.intent.new` in a directory a path unit
    # is watching is a file an operator has to explain.
    assert [path.name for path in directory.iterdir()] == [manager_module.INTENT_NAME]


def test_no_updater_is_a_sentence_and_not_a_button(tmp_path: Path) -> None:
    """A container, a checkout and a `pip install` all land here.

    The honest answer is the documented upgrade path, not a button that writes
    a file nothing will ever read.
    """
    link = ManagerLink(tmp_path / "nothing-here")
    assert not link.installed
    with pytest.raises(ManagerError, match="no local updater|there is no"):
        link.request("0.5.0", "http://127.0.0.1:8000/healthz")


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


def service(tmp_path: Path, **kwargs: object) -> SelfUpdateService:
    return SelfUpdateService(
        ManagerLink(ipc(tmp_path)),
        version=kwargs.pop("version", __version__),  # type: ignore[arg-type]
        health="http://127.0.0.1:8000/api/v1/healthz",
        state_dir=tmp_path / "state",
        **kwargs,  # type: ignore[arg-type]
    )


def test_read_only_refuses_to_update_itself(tmp_path: Path) -> None:
    """The largest change this Controller can make is to itself."""
    with pytest.raises(ManagerError, match="read-only"):
        service(tmp_path, read_only=True).request()


def test_two_updates_at_once_is_refused_with_a_reason(tmp_path: Path) -> None:
    """Two managers renaming the same file is not a state anyone could
    describe, and the second click is the one that would cause it."""
    updater = service(tmp_path)
    (updater.link.directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="probation")
    )
    with pytest.raises(ManagerError, match="already"):
        updater.request()


def test_a_finished_run_does_not_block_the_next_one(tmp_path: Path) -> None:
    updater = service(tmp_path)
    for phase in ("success", "failed", "rolled_back"):
        (updater.link.directory / manager_module.STATUS_NAME).write_text(
            status_document(phase=phase)
        )
        updater.request()


# --------------------------------------------------------------------------
# The cascade
# --------------------------------------------------------------------------


async def cascade_of(updater: SelfUpdateService) -> list[str]:
    """Run the watcher until it either cascades or gives up, and report."""
    started: list[str] = []

    async def rollout(version: str) -> None:
        started.append(version)

    updater.start(rollout)
    for _ in range(50):
        await asyncio.sleep(0)
        if started:
            break
    await updater.aclose()
    return started


async def test_an_update_this_machine_performed_rolls_the_fleet_forward(tmp_path: Path) -> None:
    """Phase two, and the only case it fires in.

    The manager installed *this* version and left signed agents for it, so the
    fleet is behind by exactly the release that is now on the disk.
    """
    updater = service(tmp_path)
    (updater.link.directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="success", version=__version__, cascade=__version__)
    )
    assert await cascade_of(updater) == [__version__]


async def test_a_cascade_naming_another_version_is_a_stale_sentence(tmp_path: Path) -> None:
    """A status file left from a run that rolled back names a version this
    Controller is not. Acting on it would start a fleet-wide rollout on the
    strength of a report about a machine in a different state."""
    updater = service(tmp_path)
    (updater.link.directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="success", version="9.9.9", cascade="9.9.9")
    )
    assert await cascade_of(updater) == []


async def test_a_run_that_failed_cascades_nothing(tmp_path: Path) -> None:
    updater = service(tmp_path)
    (updater.link.directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="rolled_back", version=__version__, cascade=__version__)
    )
    assert await cascade_of(updater) == []


async def test_a_restart_is_not_a_second_rollout(tmp_path: Path) -> None:
    """The status file is root's and outlives every restart, so "have I
    already acted on this?" has to be recorded on this side."""
    updater = service(tmp_path)
    (updater.link.directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="success", version=__version__, cascade=__version__)
    )
    assert await cascade_of(updater) == [__version__]

    again = SelfUpdateService(
        updater.link,
        version=__version__,
        health="http://127.0.0.1:8000/api/v1/healthz",
        state_dir=tmp_path / "state",
    )
    assert await cascade_of(again) == []


async def test_files_appearing_in_the_release_directory_do_not_cascade(tmp_path: Path) -> None:
    """An operator who copies artifacts in by hand gets what they always got:
    a release the dashboard offers and a button to roll it out.

    Only an update *this machine performed* starts one without being asked,
    because only then is there evidence about what changed and why -- and that
    evidence is a status file root wrote.
    """
    updater = service(tmp_path)
    assert await cascade_of(updater) == []


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------


def test_the_dashboard_is_told_why_there_is_no_button(tmp_path: Path) -> None:
    """An install path that silently has no button looks broken."""
    controller = make_controller(tmp_path / "state", manager_ipc_dir=str(tmp_path / "absent"))
    with TestClient(controller.ui) as client:
        body = client.get(f"{API_PREFIX}/controller").json()
    assert body["version"] == __version__
    assert body["updatable"] is False
    assert "container" in body["reason"]
    assert body["update"] is None


def test_asking_for_an_update_writes_the_intent(tmp_path: Path) -> None:
    directory = ipc(tmp_path)
    controller = make_controller(
        tmp_path / "state", read_only=False, manager_ipc_dir=str(directory)
    )
    with TestClient(controller.ui) as client:
        response = client.post(f"{API_PREFIX}/controller/update", json={"version": "0.5.0"})
    assert response.status_code == 200
    assert "version 0.5.0" in (directory / manager_module.INTENT_NAME).read_text()


def test_the_health_url_the_manager_is_given_is_loopback(tmp_path: Path) -> None:
    """The manager refuses anything else, and it is right to.

    A probation check that can be pointed at another machine is one that always
    passes -- so a Controller bound to `0.0.0.0` still names `127.0.0.1` here.
    """
    directory = ipc(tmp_path)
    controller = make_controller(
        tmp_path / "state", read_only=False, manager_ipc_dir=str(directory)
    )
    controller.settings.api.host = "0.0.0.0"  # noqa: S104 - the case being tested
    with TestClient(controller.ui) as client:
        client.post(f"{API_PREFIX}/controller/update", json={})
    document = (directory / manager_module.INTENT_NAME).read_text()
    assert "health http://127.0.0.1:" in document


def test_a_read_only_controller_says_so_rather_than_writing(tmp_path: Path) -> None:
    directory = ipc(tmp_path)
    controller = make_controller(tmp_path / "state", manager_ipc_dir=str(directory))
    with TestClient(controller.ui) as client:
        response = client.post(f"{API_PREFIX}/controller/update", json={})
    assert response.status_code == 409
    assert "read-only" in response.json()["detail"]
    assert not (directory / manager_module.INTENT_NAME).exists()


def test_progress_is_whatever_root_wrote(tmp_path: Path) -> None:
    """No state is held between the calls, because the middle of this
    operation is this process being stopped and replaced."""
    directory = ipc(tmp_path)
    controller = make_controller(tmp_path / "state", manager_ipc_dir=str(directory))
    (directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="probation", version="0.5.0", detail="Waiting for it to answer.")
    )
    with TestClient(controller.ui) as client:
        body = client.get(f"{API_PREFIX}/controller/update").json()
    assert body["phase"] == "probation"
    assert body["running"] is True
    assert body["detail"] == "Waiting for it to answer."


async def test_a_cascade_waits_for_the_fleet_to_dial_back_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first look after a self-update finds nobody, and that is normal.

    A rollout is planned from the hosts connected *at that instant*, and this
    process was started by the updater a second ago — every agent is still
    re-dialling. Treating that first `UpgradeUnavailable` as the answer records
    the cascade as done and the fleet silently never moves.

    Found by running the whole thing: Controller updated, agents downloaded,
    both hosts upgradable, `rollout: None`, and one line in the journal.
    """
    from bystack.runtime import selfupdate as selfupdate_module
    from bystack.runtime.upgrade import UpgradeUnavailable

    # The product waits two seconds between looks, which is right for a fleet
    # and wrong for a test that would otherwise spend six.
    monkeypatch.setattr(selfupdate_module, "POLL_SECONDS", 0.001)

    directory = ipc(tmp_path)
    (directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="success", version=__version__, cascade=__version__)
    )
    service = SelfUpdateService(
        ManagerLink(directory),
        version=__version__,
        health="http://127.0.0.1:8000/api/v1/healthz",
        state_dir=tmp_path / "state",
    )

    attempts: list[str] = []

    async def rollout(version: str) -> None:
        attempts.append(version)
        # Nobody is back yet on the first two looks.
        if len(attempts) < 3:
            raise UpgradeUnavailable("no connected host needs it")

    service.start(rollout)
    for _ in range(400):
        if len(attempts) >= 3:
            break
        await asyncio.sleep(0.005)
    await service.aclose()

    assert len(attempts) >= 3, f"it gave up after {len(attempts)} attempt(s)"
    assert (tmp_path / "state" / "cascaded").read_text().strip() == __version__, (
        "the cascade is only recorded once a rollout has actually begun"
    )


async def test_a_cascade_that_is_genuinely_refused_is_not_retried_forever(
    tmp_path: Path,
) -> None:
    """Anything that is not "not yet" will refuse again on every tick.

    Recorded so a Controller does not spend its whole cascade window
    re-attempting a rollout that cannot start, and does not try again on every
    restart after that.
    """
    directory = ipc(tmp_path)
    (directory / manager_module.STATUS_NAME).write_text(
        status_document(phase="success", version=__version__, cascade=__version__)
    )
    service = SelfUpdateService(
        ManagerLink(directory),
        version=__version__,
        health="http://127.0.0.1:8000/api/v1/healthz",
        state_dir=tmp_path / "state",
    )

    attempts: list[str] = []

    async def rollout(version: str) -> None:
        attempts.append(version)
        raise RuntimeError("the release directory is on fire")

    service.start(rollout)
    for _ in range(400):
        if (tmp_path / "state" / "cascaded").exists():
            break
        await asyncio.sleep(0.005)
    await service.aclose()

    assert attempts == [__version__], f"it retried a hard refusal: {attempts}"
    assert (tmp_path / "state" / "cascaded").exists()
