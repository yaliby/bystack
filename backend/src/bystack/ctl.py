"""`bystack-ctl` — the operator's four decisions, without a browser.

    bystack-ctl status                 # is this Controller healthy, and what does it see
    bystack-ctl hosts                  # the fleet
    bystack-ctl token                  # mint a join token, print the install command
    bystack-ctl approve <engine-id>
    bystack-ctl revoke <engine-id>
    bystack-ctl releases               # signed agent releases this Controller holds
    bystack-ctl upgrade                # roll the newest one out, one host at a time

ADR-0011 put the operator surface on four REST routes and MIGRATION §6.4 said
a CLI over them was packaging work. This is it, and it is deliberately nothing
more: every command here is one request to one route, and there is no state,
no cache and no second opinion about anything. The dashboard drives the same
routes; neither is the source of truth, because the Controller is.

**Why it exists when there is already a dashboard.** The dashboard is on
loopback by default, which means the person adding a host over ssh cannot
reach it without forwarding a port. And the two things an operator most wants
to automate -- mint a token, approve what comes back -- are exactly the two
that are painful to do by clicking.

**`urllib`, not `httpx`.** The runtime `httpx` dependency was deleted when the
agentless path went (MIGRATION §6), because the Controller stopped making
outbound HTTP calls. Putting an HTTP client back into every installation for a
program most people run twice would undo that for a convenience, and the
standard library is entirely adequate for five JSON requests.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

from bystack import __version__
from bystack.api.app import API_PREFIX
from bystack.config import Settings

#: Where the Controller is, when nobody says. The API's own default bind.
DEFAULT_URL = "http://127.0.0.1:8000"

#: Long enough for a Controller that is busy, short enough that a wrong address
#: is an error rather than a hang. Nothing here is a long-running request:
#: minting a token is a signature and listing agents is a dictionary.
TIMEOUT = 10.0


class Failed(Exception):
    """Something the operator needs to read, not a traceback."""


def base_url(args: argparse.Namespace) -> str:
    """Where to talk to, from the flag, the environment, or the config file.

    Reading the *Controller's own* config is the interesting one and is why
    `--config` exists: an operator on the Controller's host already has a file
    naming the port, and asking them to repeat it in a flag is asking them to
    keep two copies of one fact in step.

    A configured `0.0.0.0` becomes loopback, because that is a bind address
    rather than a destination -- "everywhere" is not somewhere to connect to,
    and a CLI that dialled it would fail in a way that reads like the
    Controller is down.
    """
    # Named with types rather than read straight off the namespace: argparse
    # hands back `Any`, and this function's whole job is to return one string.
    flag: str | None = args.url
    config: str | None = args.config
    if flag:
        return flag
    if config:
        settings = Settings.load(config)
        host = settings.api.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{settings.api.port}"
    return os.environ.get("BYSTACK_URL", DEFAULT_URL)


def call(url: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    """One request, and an error a person can act on.

    Every failure mode here has a different answer, so they are told apart
    rather than collapsed into "request failed": a refused connection means the
    Controller is not running or is somewhere else, a 404 on an agent means the
    engine id is wrong, and a 4xx from the API carries a `detail` that was
    written to be read.
    """
    request = urllib.request.Request(
        urljoin(url, f"{API_PREFIX}{path}"),
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read() or "null")
    except urllib.error.HTTPError as exc:
        detail = ""
        # A body we cannot parse is not a reason to lose the status line. The
        # fallback below names the code and the route, which is enough to act
        # on; raising out of the error handler would replace a bad message
        # with a traceback.
        with contextlib.suppress(ValueError, OSError):
            detail = json.loads(exc.read()).get("detail", "")
        raise Failed(detail or f"{exc.code} {exc.reason} from {path}") from exc
    except urllib.error.URLError as exc:
        raise Failed(
            f"cannot reach a Controller at {url} ({exc.reason}).\n"
            "  Is it running? Pass --url, or --config /etc/bystack/bystack.yaml."
        ) from exc


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, url: str) -> int:
    health = call(url, "/healthz")
    terms = call(url, "/agents/enrollment")

    if args.json:
        print(json.dumps({"health": health, "enrollment": terms}, indent=2))
        return 0

    print(f"controller  {url}  {health['version']}  ({health['status']})")
    print(f"graph       {health['node_count']} node(s), {health['edge_count']} edge(s)")
    print(f"read only   {'yes' if health['read_only'] else 'no'}")
    local = terms["local_agent"]
    print(f"this host   {local['state']} — {local['detail']}")
    print(
        f"fleet       {'listening on ' + terms['listen'] if terms['enabled'] else 'off'}"
        f"{'  (auto-approve on)' if terms['auto_approve'] else ''}"
    )
    for provider in health["providers"]:
        print(f"  {provider['id']:<24} {provider['state']}")
    # A Controller that is up but sees nothing is the failure this exists to
    # distinguish from one that is down, so it is answered rather than left as
    # an empty list to interpret.
    if not health["providers"]:
        print("  (no providers — nothing is being observed)")
    return 0


def cmd_hosts(args: argparse.Namespace, url: str) -> int:
    agents = call(url, "/agents")
    if args.json:
        print(json.dumps(agents, indent=2))
        return 0

    if not agents:
        print("no hosts. Mint a token with `bystack-ctl token` to add one.")
        return 0

    # Measured rather than assumed. Engine IDs are a UUID on Docker 25 and
    # later and a 64-hex string before it, normalized to 25 characters
    # (`core/identity.py`) -- so any constant here is wrong for one of them,
    # and a column that is too narrow does not truncate, it just destroys the
    # alignment of every row after the longest one.
    width = max(len("ENGINE ID"), *(len(agent["engine_id"]) for agent in agents))
    current = call(url, "/healthz")["version"]
    print(f"{'ENGINE ID':<{width}} {'STATUS':<10} {'LINK':<12} {'VERSION':<10} CERTIFICATE")
    for agent in agents:
        link = "connected" if agent["connected"] else "offline"
        if agent["local"]:
            link += " (local)"
        version = agent["agent_version"] or "-"
        print(
            f"{agent['engine_id']:<{width}} {agent['status']:<10} {link:<12} "
            f"{version + ('*' if _behind(agent, current) else ''):<10} "
            f"{_expiry(agent['certificate_expires_at'])}"
        )
    # The two things that need a person, each said once at the bottom rather
    # than as a symbol in a column somebody has to interpret.
    pending = [agent for agent in agents if agent["status"] == "pending"]
    if pending:
        print()
        print(f"{len(pending)} awaiting approval. They contribute nothing until approved:")
        for agent in pending:
            print(f"  bystack-ctl approve {agent['engine_id']}")

    behind = [agent for agent in agents if _behind(agent, current)]
    if behind:
        # A mixed-version fleet is a normal operating state under ADR-0008, so
        # this is a report and not a warning. What it is not is invisible: the
        # agent version was already on this route with nothing to compare it
        # against, which made it a fact rather than an answer (ADR-0015).
        print()
        print(f"* {len(behind)} host(s) behind the Controller ({current}).")
        _how_to_upgrade(url)
    return 0


def _how_to_upgrade(url: str) -> None:
    """The one line that actually moves those hosts, whichever line it is.

    Two answers, and which one applies is a fact about this Controller rather
    than a preference: a Controller holding a signed release pushes it down the
    connections it already has (ADR-0017), and one holding nothing cannot, so
    the hosts are upgraded by running the installer on them (ADR-0015). Printing
    both would be printing one wrong instruction every time.
    """
    releases = call(url, "/agents/releases")
    if releases["releases"] and releases["upgradable"]:
        print(f"  {len(releases['upgradable'])} of them can take a pushed release:")
        print("    bystack-ctl upgrade")
        if releases["stale"]:
            # Named, because these are the hosts a rollout will *not* touch and
            # the operator would otherwise read the run as having finished.
            print(f"  {len(releases['stale'])} cannot, and need the installer once:")
            print(f"    {call(url, '/agents/enrollment')['upgrade']}")
        return

    # The command the Controller composes, not one written here. It carries the
    # running version and the address agents actually dial, so an operator who
    # upgraded the Controller ten minutes ago is handed the matching agent
    # rather than a line with an ellipsis they reconstruct on every host.
    print("  On each one:")
    print(f"    {call(url, '/agents/enrollment')['upgrade']}")
    # Said out loud because the missing token is the part that looks like an
    # omission. Re-running the installer *with* one makes the agent enrol
    # again, and the host comes back as a stranger waiting for approval while
    # the row above it goes quiet.
    print("  No token: that is what makes it an upgrade rather than a second host.")


def _behind(agent: dict[str, Any], controller: str) -> bool:
    """Whether this agent is running something other than the Controller's version.

    String inequality, not a version comparison. Parsing semver here would mean
    deciding what `0.2.0-rc1` is relative to `0.2.0` in a CLI that has no stake
    in the answer, and every wrong guess is a host reported as current when it
    is not. "Different from the Controller" is the question an operator is
    actually asking, and it has no edge cases.

    An agent that has never connected reports no version at all. That is
    unknown rather than behind, and saying otherwise would put every offline
    host in a list of things to go and fix.
    """
    version: str = agent["agent_version"]
    return bool(version) and version != controller


def cmd_token(args: argparse.Namespace, url: str) -> int:
    minted = call(url, "/agents/tokens", method="POST", body={"ttl_minutes": args.ttl})
    terms = call(url, "/agents/enrollment")

    if args.json:
        print(json.dumps(minted, indent=2))
        return 0

    print(minted["manual"] if args.manual else minted["install"])
    print()
    # Minutes, not a date. A join token lives for fifteen of them by design,
    # and "2026-08-09 (0d)" is a true statement that answers nothing.
    print(f"Expires in {_minutes(minted['expires_at'])}, single use.")
    print("Shown once — the Controller keeps only a digest of it.")
    if not terms["enabled"]:
        # Minting succeeds whether or not the listener is bound, which is the
        # exact gap `GET /agents/enrollment` was added to close for the UI. A
        # CLI that printed a command with nothing to dial would be worse: at
        # least the dialog is looked at by someone who is already here.
        print()
        print(f"WARNING: agents.enabled is false, so nothing is listening on {terms['listen']}.")
        print("This command will fail at the dial, not at the token.")
    return 0


def cmd_approve(args: argparse.Namespace, url: str) -> int:
    agent = call(url, f"/agents/{args.engine_id}/approve", method="POST")
    print(f"{agent['engine_id']} is {agent['status']}")
    print("Takes effect on its next connection attempt, which is seconds away.")
    return 0


def cmd_revoke(args: argparse.Namespace, url: str) -> int:
    agent = call(url, f"/agents/{args.engine_id}/revoke", method="POST")
    print(f"{agent['engine_id']} is {agent['status']}")
    # Said plainly because the opposite is a reasonable thing to assume, and
    # assuming it during an incident is expensive.
    print("A connected agent keeps its current stream; this refuses the next one.")
    return 0


def cmd_releases(args: argparse.Namespace, url: str) -> int:
    answer = call(url, "/agents/releases")
    if args.json:
        print(json.dumps(answer, indent=2))
        return 0

    print(f"releases in {answer['directory']}")
    if not answer["releases"]:
        # The empty case is the common one and is not a fault: a Controller
        # that distributes nothing is a Controller whose hosts are upgraded by
        # the installer, which is what every one of them did to get here.
        print("  (none — build with scripts/build-agent.sh, sign with scripts/sign-agent.py)")
    for release in answer["releases"]:
        size = release["size"] / (1024 * 1024)
        print(f"  {release['version']:<12} {release['arch']:<10} {size:.1f} MiB  "
              f"{_expiry(release['released_at']).split(' ')[0]}")

    print()
    print(f"{len(answer['upgradable'])} host(s) can take a pushed release")
    if answer["stale"]:
        # Not a fault either, and the distinction is the whole reason this line
        # exists: these hosts are managed, healthy and simply running an agent
        # from before signed push existed. A rollout steps over them silently
        # unless something says so.
        print(f"{len(answer['stale'])} cannot, and need install-agent.sh run on them once:")
        for engine_id in answer["stale"]:
            print(f"  {engine_id}")
    return 0


def cmd_upgrade(args: argparse.Namespace, url: str) -> int:
    """Start, watch or stop the fleet rollout.

    One flag-shaped command rather than three subcommands, because there is
    only ever one rollout: `bystack-ctl upgrade` starts it, and the other two
    are questions about the same object.
    """
    if args.cancel:
        rollout = call(url, "/agents/upgrades/cancel", method="POST")
        print(f"stopping after {rollout['current'] or 'the current host'}.")
        print("A host mid-transfer has written a partial file and nothing else.")
        return 0

    if args.status:
        rollout = call(url, "/agents/upgrades")
        if rollout is None:
            print("no rollout has been started.")
            return 0
        return _report(rollout, args)

    rollout = call(
        url, "/agents/upgrades", method="POST", body={"version": args.version or ""}
    )
    if args.json and not args.watch:
        print(json.dumps(rollout, indent=2))
        return 0

    print(f"rolling out {rollout['version']} to {len(rollout['planned'])} host(s), one at a time.")
    print("A failure stops the run rather than completing it.")
    if not args.watch:
        print("  bystack-ctl upgrade --status")
        return 0

    # Polled rather than streamed. There is no event for this and there should
    # not be one: a rollout is minutes long and the thing an operator wants is
    # the same answer the panel shows, not a socket.
    while rollout["state"] == "running":
        time.sleep(2)
        rollout = call(url, "/agents/upgrades")
    return _report(rollout, args)


def _report(rollout: dict[str, Any], args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(rollout, indent=2))
    else:
        done = sum(1 for r in rollout["results"] if r["state"] == "confirmed")
        print(
            f"{rollout['version']}: {rollout['state']}  "
            f"({done} of {len(rollout['planned'])} confirmed)"
        )
        for result in rollout["results"]:
            reason = f"  {result['reason']}" if result["reason"] else ""
            print(f"  {result['engine_id']:<26} {result['state']:<10}{reason}")
        if rollout["current"]:
            print(f"  {rollout['current']:<26} in progress")
        if rollout["detail"]:
            print()
            print(rollout["detail"])
    # A run that stopped on a host is a non-zero exit, so a script that ran
    # this does not go on to report a fleet upgrade that did not happen.
    return 1 if rollout["state"] == "failed" else 0


def _minutes(unix: int) -> str:
    """How long is left, for something whose whole lifetime is minutes."""
    left = unix - int(dt.datetime.now(tz=dt.UTC).timestamp())
    if left <= 0:
        return "no time at all — it has already expired"
    if left < 60:
        return f"{left} seconds"
    return f"{left // 60} minutes"


def _expiry(unix: int) -> str:
    """A timestamp as a date and a distance.

    Both, because they answer different questions and an operator reading a
    fleet list is usually asking the second one. Zero means there is no
    certificate at all -- the Controller's own agent never enrolled -- and
    printing the epoch for that would be a fabricated fact.
    """
    if not unix:
        return "—"
    when = dt.datetime.fromtimestamp(unix, tz=dt.UTC)
    days = (when - dt.datetime.now(tz=dt.UTC)).days
    return f"{when:%Y-%m-%d} ({'expired' if days < 0 else f'{days}d'})"


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bystack-ctl",
        description="Talk to a ByStack Controller.",
    )
    parser.add_argument("--url", help=f"Controller base URL (default {DEFAULT_URL})")
    parser.add_argument("--config", help="Read the address out of a Controller config file")
    parser.add_argument("--json", action="store_true", help="Raw JSON instead of a table")
    parser.add_argument("--version", action="version", version=f"bystack-ctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Health, what is observed, and whether hosts can join")
    sub.add_parser("hosts", help="Every host this Controller manages")

    token = sub.add_parser("token", help="Mint a join token and print the install command")
    token.add_argument("--ttl", type=int, default=15, help="Minutes (default 15)")
    token.add_argument(
        "--manual",
        action="store_true",
        help="Print the command for a host that already has the agent binary",
    )

    sub.add_parser("releases", help="Signed agent releases this Controller can distribute")

    upgrade = sub.add_parser("upgrade", help="Roll a release out to the fleet, one host at a time")
    upgrade.add_argument("--version", help="Which release (default: the newest held)")
    upgrade.add_argument("--watch", action="store_true", help="Follow the run to the end")
    upgrade.add_argument("--status", action="store_true", help="Report the current or last run")
    upgrade.add_argument("--cancel", action="store_true", help="Stop after the current host")

    for name, help_text in (
        ("approve", "Let an enrolled agent contribute to the graph"),
        ("revoke", "Stop trusting this host's certificate"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("engine_id")

    return parser


COMMANDS = {
    "status": cmd_status,
    "releases": cmd_releases,
    "upgrade": cmd_upgrade,
    "hosts": cmd_hosts,
    "token": cmd_token,
    "approve": cmd_approve,
    "revoke": cmd_revoke,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, base_url(args))
    except Failed as exc:
        print(f"bystack-ctl: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"bystack-ctl: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
