"""What goes into the wheel besides the Python.

`pip install bystack` has to be enough to manage a machine. That means the
wheel carries the two artifacts the Controller looks for at runtime and cannot
build for itself:

* ``bystack/_bundled/bystack-agent`` — the agent it spawns for its own engine
  (`runtime/localagent.py`). Without it a first run has no Docker socket to
  read and says so, which is honest and is still an empty canvas.
* ``bystack/_web`` — the dashboard (`api/web.py`). Without it the API runs and
  the browser gets a page explaining how to build one.

Both are **optional**, deliberately. A wheel built in a checkout where neither
`cargo build` nor `npm run build` has run is a working Controller for a fleet
that enrolls its own agents, and refusing to build one would make the test
suite's own packaging job depend on a Rust toolchain. What is not optional is
saying which kind of wheel came out, which is what the notes below print.

Nothing is copied into `src/`. Files are force-included from where they
already are, so the source tree after a build is the source tree before it --
no ignore rules to keep in step, and no chance of a stale binary from last
month being picked up by the next build.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BACKEND = Path(__file__).resolve().parent
REPO = BACKEND.parent

#: Where `scripts/build-agent.sh` leaves its output, and `npm run build` its own.
AGENT_DIST = REPO / "dist"
WEB_DIST = REPO / "frontend" / "dist"

#: Python spells some architectures differently from `uname -m`, which is what
#: the agent binaries are named after. Only the disagreements are listed.
_ARCH_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}


def _target_arch() -> str:
    """Which architecture this wheel is being built for.

    Overridable because cross-building the wheel is ordinary -- the release
    job builds every architecture on one runner -- and `platform.machine()`
    answers for the runner rather than for the target.
    """
    arch = os.environ.get("BYSTACK_TARGET_ARCH") or platform.machine()
    return _ARCH_ALIASES.get(arch.lower(), arch.lower())


def _agent_binary(arch: str) -> Path | None:
    """The agent to bundle, if one has been built.

    An explicit path wins and is never fallen back from, the rule every other
    lookup in this project follows: a release that silently shipped last
    week's binary because this week's was named slightly differently is a
    failure nobody would notice until a fleet stopped reconnecting.
    """
    override = os.environ.get("BYSTACK_BUNDLE_AGENT")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"BYSTACK_BUNDLE_AGENT={path} is not a file")
        return path

    candidate = AGENT_DIST / f"bystack-agent-{arch}"
    return candidate if candidate.is_file() else None


class BundleArtifacts(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        arch = _target_arch()
        agent = _agent_binary(arch)
        included: list[str] = []

        if agent is not None:
            build_data["force_include"][str(agent)] = "bystack/_bundled/bystack-agent"
            included.append(f"agent ({arch}, {agent.stat().st_size // 1024} KiB)")

            # A wheel holding a binary is not a pure-Python wheel, and tagging
            # it `any` is how an aarch64 host installs an x86_64 agent and
            # discovers it at the first spawn.
            #
            # The tag is a *compressed set* of two, and both are honest
            # understatements: the binary is static musl, so it needs no libc
            # from the host at all, and there is no tag that says so. Claiming
            # glibc 2.17 and musl 1.1 together covers every Linux pip runs on
            # while saying something true about each. `linux_{arch}` would be
            # the literal truth and is rejected by PyPI, which is not a reason
            # to prefer it here but is a reason not to be the only option.
            build_data["pure_python"] = False
            build_data["tag"] = (
                f"py3-none-manylinux_2_17_{arch}.musllinux_1_1_{arch}"
            )

        if (WEB_DIST / "index.html").is_file():
            build_data["force_include"][str(WEB_DIST)] = "bystack/_web"
            included.append("dashboard")

        # Printed rather than logged, because the interesting case is the
        # quiet one: a release that shipped a Controller with no agent in it
        # looks exactly like a release that shipped one, until a first run
        # somewhere else comes up empty.
        if included:
            print(f"bystack: wheel includes {', '.join(included)}")
        else:
            print(
                "bystack: pure wheel -- no agent binary in dist/ and no frontend "
                "build in frontend/dist. The Controller will look on PATH and in "
                "the checkout at runtime; see scripts/build-agent.sh."
            )
