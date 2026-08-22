"""The one number everything published together carries, and where it rots.

`__version__` is not a label. The Controller composes the "add a host" and
"upgrade" commands out of it (`routes/enrollment.py`), so it decides which
release tag an operator's pasted command fetches `install-agent.sh` and the
agent binary from. That makes every *other* written copy of the number a way to
publish a broken command:

- **A build file left behind** — `agent/Cargo.toml` or `backend/pyproject.toml`
  — produces artifacts whose names disagree with the tag they are published
  under, and a wheel that reports a version the Controller inside it does not.
- **`scripts/install-agent.sh`'s `VERSION` default left behind** is the worst
  of them, because it fails quietly and correctly: the operator pastes the
  right URL, the script it fetches installs the *previous* release, and the
  host comes up healthy and behind.
- **A document left behind** hands out a URL to a tag that either does not
  exist (404, at the moment somebody is adding their first server) or exists
  and is old.

This is the same rule as `test_wire.py::test_the_constants_mirrored_across_
languages_still_agree`: a number written down twice gets a guard rather than a
comment. It is a separate file because these copies are not on the wire — they
are the release, and the failure is on somebody else's machine rather than
between two of our processes.

Reading the literals rather than templating them is deliberate. A document with
a placeholder in it is a document nobody can paste from, and pasteable is the
whole point of the ones scanned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bystack import __version__

ROOT = Path(__file__).resolve().parent.parent.parent

#: Files that carry the number in a form an operator or a machine acts on.
#: `CHANGELOG.md` is deliberately absent from the pattern scan below -- naming
#: old versions in prose is what it is for -- and gets its own check instead.
DOCUMENTS = (
    "README.md",
    "INSTALL.md",
    "install.html",
    "scripts/install-agent.sh",
    "scripts/install-controller.sh",
    ".github/workflows/release.yml",
)

#: The installers, whose `VERSION` default is what they fetch when nobody
#: overrides it. Both compose their download URLs out of it rather than out of
#: the URL they were themselves fetched from, so a stale default is a correctly
#: pasted command that installs the previous release.
INSTALLERS = ("scripts/install-agent.sh", "scripts/install-controller.sh")

#: Every shape in which a version is written down somewhere it will be acted
#: on, with the version as the one capture group.
PATTERNS = (
    # The installer's URL, which is what the Controller composes and what the
    # documents tell an operator to paste.
    re.compile(r"raw\.githubusercontent\.com/yaliby/bystack/v([0-9][^/\s]*)/"),
    # Release assets fetched by tag.
    re.compile(r"releases/download/v([0-9][^/\s]*)/"),
    # Wheel filenames, which a `pip install` line names in full.
    re.compile(r"bystack-([0-9][0-9A-Za-z.]*)-"),
    # The command that cuts the release, quoted as an example in two places.
    # Both halves of it: `git tag v0.5.0 && git push origin v0.4.0` is a real
    # thing a bump left behind, it reads correctly at a glance, and it pushes a
    # tag that has nothing to do with the release being cut.
    re.compile(r"git tag v([0-9][0-9A-Za-z.]*)"),
    re.compile(r"git push origin v([0-9][0-9A-Za-z.]*)"),
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_build_files_carry_the_controllers_version() -> None:
    """`__version__` is the authority; these two are copies of it.

    The agent's version reaches the Controller over the wire and is what the
    Hosts panel compares against, so a `Cargo.toml` that did not move makes
    every upgraded host report itself as still behind -- and the operator runs
    the upgrade again, on a host that is already current.
    """
    cargo = re.search(r'^version = "([^"]+)"', _read("agent/Cargo.toml"), re.M)
    assert cargo and cargo.group(1) == __version__, (
        f"agent/Cargo.toml is {cargo.group(1) if cargo else 'unreadable'}, "
        f"bystack.__version__ is {__version__}"
    )

    pyproject = re.search(r'^version = "([^"]+)"', _read("backend/pyproject.toml"), re.M)
    assert pyproject and pyproject.group(1) == __version__, (
        f"backend/pyproject.toml is {pyproject.group(1) if pyproject else 'unreadable'}, "
        f"bystack.__version__ is {__version__}"
    )


@pytest.mark.parametrize("installer", INSTALLERS)
def test_the_installers_default_version_is_this_release(installer: str) -> None:
    """The copy that fails silently.

    Both installers fetch their binaries and `SHA256SUMS` from `VERSION`, not
    from the URL they were themselves fetched from. A stale default means the
    right script installs the wrong software and says nothing -- a fleet
    uniformly one release behind, or a Controller installed at a version whose
    dashboard hands out a different one.

    It is worse on the Controller's side than the agent's. The version it
    installs is the anti-downgrade floor for every update after it
    (`bystack-manager` reads `--version` off the file that actually runs), so a
    machine installed a release ahead of where it should be is one that refuses
    the release it was supposed to get.
    """
    default = re.search(r'^VERSION="\$\{BYSTACK_VERSION:-v([^}"]+)\}"', _read(installer), re.M)
    assert default and default.group(1) == __version__, (
        f"{installer} defaults to v{default.group(1) if default else '?'}; "
        f"this release is v{__version__}. It would install the wrong binary from a "
        f"correctly-pasted command."
    )


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_pasteable_version_in_the_documents_is_this_one(document: str) -> None:
    """No document hands out a tag other than the one being released.

    Scanned by shape rather than by counting occurrences: the point is not how
    many copies there are -- there are eighteen -- but that a reader cannot
    reach a stale one. Adding another `curl` line to a document needs no edit
    here; getting its number wrong fails.
    """
    text = _read(document)
    found = {match for pattern in PATTERNS for match in pattern.findall(text)}
    stale = sorted(version for version in found if version != __version__)
    assert not stale, (
        f"{document} names version(s) {stale}; this release is {__version__}. "
        f"A URL pinned to a tag that is not this one 404s for a new host, or "
        f"quietly installs an older agent."
    )


def test_the_changelog_opens_on_this_release() -> None:
    """A version published with nothing said about it.

    The one document that is *supposed* to name old versions, so it is checked
    from the other end: its first heading is the release being cut. Bumping
    `__version__` without writing the entry fails here, which is the moment it
    is still cheap to write -- afterwards it is an operator upgrading into a
    changelog that stops one release short of what they just installed.
    """
    first = re.search(r"^## v(\S+)", _read("CHANGELOG.md"), re.M)
    assert first and first.group(1) == __version__, (
        f"CHANGELOG.md opens on v{first.group(1) if first else '?'}, "
        f"and this release is v{__version__}"
    )
