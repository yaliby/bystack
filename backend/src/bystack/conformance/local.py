"""Zero-config conformance: does the Controller manage its own machine?

    python -m bystack.conformance.local ../agent/target/release/bystack-agent

The other two runs check an agent that *dials in*. This one checks the thing
`python -m bystack` does with no configuration at all (`docs/MIGRATION.md`
§4): bind a unix socket, spawn the bundled agent against it, and fill the
graph — with no CA, no token, no approval and no open port anywhere.

It drives the real supervisor and the real listener, not a reconstruction of
them. `LocalAgent` spawns the process, `main.local_listener` binds the socket,
and the frames arrive at the same route an enrolled host's do. A harness that
built its own subprocess and its own socket would pass while the Controller's
own wiring was broken, which is the only way this can fail.

The scripted engine keeps the rule the whole suite keeps: **no test anywhere
requires a Docker daemon or a network.**

Both directions are checked, not just discovery: commands go out over this
transport and log reads come back over it. The other two runs prove those
against a WebSocket, which is a different carrier for the same frames -- and
the zero-config path is the one a first-run user is on.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import tempfile
from pathlib import Path

from bystack.api.app import build_context
from bystack.config import AgentsConfig, LocalAgentConfig, Settings
from bystack.conformance.engine import (
    DEFAULT_IMAGE,
    DEFAULT_NETWORK,
    DEFAULT_VOLUME,
    ScriptedEngine,
    make_container,
)
from bystack.conformance.report import Check, report
from bystack.core.identity import NodeKind, container_urn, engine_scope
from bystack.core.ports.command import CommandKind, CommandRequest
from bystack.main import local_listener
from bystack.runtime.localagent import LocalAgentState

C1 = "c" * 64

#: Generous, and for the same reason as the other runs': a failing check must
#: mean "this does not work", never "the machine was busy". Larger than theirs
#: because this one includes a process launch.
SETTLE = 10.0


async def until(predicate: object, within: float = SETTLE) -> bool:
    """Poll until true. There is nothing to await except the effect."""
    assert callable(predicate)
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


async def stopped_by_signal(agent_binary: Path) -> list[Check]:
    """Stop the Controller the way a service manager stops it.

    Every other check here drives shutdown by calling it. A service manager
    sends a signal instead, and that was a different path: uvicorn restores
    the handler it replaced and then re-raises the signal when `serve()`
    returns, so the process died before the cleanup its caller arranged could
    run, and a `systemctl stop` left the socket in the state directory.

    No in-process harness can observe that -- the bug is in what happens to
    the process. So this one spawns the real entry point and signals it, and
    is the only check in the suite that does.
    """
    workdir = Path(tempfile.mkdtemp(prefix="bystack-signal-"))
    engine = ScriptedEngine(
        workdir / "docker.sock",
        containers=[make_container(C1, "shop-web-1")],
        networks=[DEFAULT_NETWORK],
        volumes=[DEFAULT_VOLUME],
        images=[DEFAULT_IMAGE],
    )
    await engine.start()

    socket_path = workdir / "state" / "local-agent.sock"
    config = workdir / "bystack.yaml"
    # Port 0 because nothing here connects to the browser's listener; what is
    # under test is the exit, and a fixed port would make this check fail for
    # the one reason it must never fail for.
    config.write_text(
        "read_only: true\n"
        "agents:\n  enabled: false\n"
        f"  state_dir: {workdir / 'controller'}\n"
        "local_agent:\n  enabled: true\n"
        f"  binary: {agent_binary}\n"
        f"  docker_socket: {workdir / 'docker.sock'}\n"
        f"  socket: {socket_path}\n"
        "api:\n  host: 127.0.0.1\n  port: 0\n"
    )

    checks: list[Check] = []
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "bystack", "--config", str(config),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        started = await until(socket_path.exists)
        checks.append(
            Check(
                "the spawned Controller binds its socket",
                "the rest of this check means nothing if it never started",
                started,
                str(socket_path),
            )
        )

        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=SETTLE)
            exited = True
        except TimeoutError:
            exited = False
            process.kill()
            await process.wait()

        checks.append(
            Check(
                "SIGTERM stops it, and stops it cleanly",
                "a service manager sends this, and an exit status is what it reports "
                "to whoever asks why the unit is not running",
                exited and process.returncode == 0,
                f"exit status {process.returncode}" if exited else "did not exit",
            )
        )
        checks.append(
            Check(
                "and a signalled shutdown leaves no socket behind either",
                "the same claim as the check above, on the path a deployment "
                "actually takes -- `systemctl stop` is not an exotic case",
                not socket_path.exists(),
                str(socket_path),
            )
        )
    finally:
        await engine.stop()

    return checks


async def run(agent_binary: Path) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="bystack-local-"))
    engine_socket = workdir / "docker.sock"
    checks: list[Check] = []

    engine = ScriptedEngine(
        engine_socket,
        containers=[make_container(C1, "shop-web-1")],
        networks=[DEFAULT_NETWORK],
        volumes=[DEFAULT_VOLUME],
        images=[DEFAULT_IMAGE],
    )
    await engine.start()

    # Exactly what `Settings.default()` means, with the two paths pointed at
    # this run's temporary directory instead of at the machine's. Nothing else
    # is overridden -- in particular the fleet listener stays off, because the
    # claim under test is that zero-config manages this host *without* it.
    settings = Settings(
        read_only=False,
        agents=AgentsConfig(enabled=False, state_dir=str(workdir / "controller")),
        local_agent=LocalAgentConfig(
            enabled=True,
            binary=str(agent_binary),
            docker_socket=str(engine_socket),
            socket=str(workdir / "state" / "local-agent.sock"),
        ),
    )
    context = build_context(settings)
    await context.collector.start()

    print(f"engine socket : {engine_socket}")
    print(f"agent socket  : {context.local_agent.socket_path}")
    print(f"agent         : {agent_binary}\n")

    listener = local_listener(context, "warning")
    if listener is None:
        checks.append(
            Check(
                "the local listener binds",
                "without it there is no zero-config startup at all",
                False,
                context.local_agent.status.detail,
            )
        )
        return report("zero-config conformance", checks)

    server, sock = listener
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # noqa: ASYNC110 - uvicorn signals with an attribute
        await asyncio.sleep(0.05)

    # -- the socket is the credential -------------------------------------

    mode = context.local_agent.socket_path.stat().st_mode & 0o777
    parent = context.local_agent.socket_path.parent.stat().st_mode & 0o777
    checks.append(
        Check(
            "the socket is the credential",
            "this connection is admitted without a certificate, so its file mode is the "
            "entire authentication; a world-writable socket would let any local process "
            "claim to be this host's agent",
            mode == 0o600 and parent == 0o700,
            f"socket {mode:04o} in a {parent:04o} directory",
        )
    )

    await context.local_agent.start()

    try:
        # -- it fills the graph -------------------------------------------

        store = context.store
        filled = await until(
            lambda: any(n.kind == NodeKind.CONTAINER for n in store.snapshot().nodes)
        )
        checks.append(
            Check(
                "zero-config fills the graph",
                "`python -m bystack` with no configuration must manage this machine; an "
                "empty graph on first run is the regression MIGRATION section 4 names",
                filled,
                f"{len(store.snapshot().nodes)} node(s), no config file, no token",
            )
        )

        engine_id = engine_scope(str(engine.info["ID"]))
        checks.append(
            Check(
                "the same URNs as any other host",
                "the local path is a transport, not a second discovery implementation; if "
                "its identities differed, every reader above the store would need to know "
                "which kind of host it was looking at",
                store.node(container_urn(engine_id, C1)) is not None,
                container_urn(engine_id, C1),
            )
        )

        # -- and it is not enrolled ---------------------------------------

        provider = context.collector.agent_provider(engine_id, create=False)
        checks.append(
            Check(
                "managed without enrolling",
                "an enrollment record is durable operator intent (ADR-0001); a Controller "
                "that wrote one per machine it ever ran on would accumulate rows nobody "
                "approved and nobody can revoke",
                provider is not None
                and provider.local
                and context.trust.registry.all() == [],
                f"{len(context.trust.registry.all())} enrolled agent(s)",
            )
        )

        # -- commands reach the engine ------------------------------------

        result = await context.commands.execute(
            CommandRequest(
                kind=CommandKind.STOP, target=container_urn(engine_id, C1), reason="conformance"
            )
        )
        stopped = any(c["Id"] == C1 and c["State"] == "exited" for c in engine.containers)
        checks.append(
            Check(
                "commands reach the local engine",
                "the map is the control surface, and a host discovered locally that could "
                "only be looked at would be half a host",
                result.ok and stopped,
                f"{result.status}: {result.outcomes[0].detail or 'ok'}",
            )
        )

        recorded = context.commands.audit.recent(10)
        checks.append(
            Check(
                "and are recorded where an operator can see them",
                "an operation that happened and left no trace on the timeline is one the "
                "next person cannot account for; the audit ring is what the Activity panel "
                "reads, and it records refusals too",
                len(recorded) == 1 and recorded[0].target == container_urn(engine_id, C1),
                f"{len(recorded)} entr(y/ies), status {recorded[0].status if recorded else '-'}",
            )
        )

        # -- and reads come back the same way -----------------------------

        engine.logs[C1] = [
            (False, "listening on :80"),
            (True, "upstream timed out"),
            (False, "shutting down"),
        ]
        answer = await provider.logs(C1, 100) if provider is not None else None
        tagged = [(line.stderr, line.text) for line in answer.lines] if answer else []
        checks.append(
            Check(
                "a log read crosses the local socket",
                "the request/response correlation and Docker's stream framing are proven "
                "over the WebSocket the other two runs use; this is the only suite that "
                "carries them over the unix socket a first-run user actually has, and "
                "'why did this container die' is the first thing they will ask",
                answer is not None
                and answer.ok
                and (True, "upstream timed out") in tagged
                and (False, "listening on :80") in tagged,
                (answer.reason if answer is not None else "no provider")
                or f"{len(tagged)} line(s), {sum(1 for stderr, _ in tagged if stderr)} on stderr",
            )
        )

        tailed = await provider.logs(C1, 1) if provider is not None else None
        checks.append(
            Check(
                "and the tail is bounded on this path too",
                "an unbounded read is how a month-old log exhausts an agent, and the "
                "local agent is the one sharing a machine with the Controller",
                tailed is not None and tailed.ok and len(tailed.lines) < 3,
                f"{len(tailed.lines) if tailed else '-'} line(s) for tail=1",
            )
        )

        # -- the supervisor supervises ------------------------------------

        killed = context.local_agent._process  # noqa: SLF001 - the point of the check
        if killed is not None:
            killed.kill()
        recovered = await until(
            lambda: context.local_agent.status.state is LocalAgentState.RUNNING
            and context.local_agent._process is not killed,  # noqa: SLF001
            15.0,
        )
        checks.append(
            Check(
                "a dead agent is restarted",
                "an agent that dies once and stays dead turns the operator's own machine "
                "into a host that silently stops updating, with nothing on the canvas to "
                "say so",
                recovered,
                context.local_agent.status.detail,
            )
        )

        resynced = await until(
            lambda: store.node(container_urn(engine_id, C1)) is not None
            and (
                p := context.collector.agent_provider(engine_id, create=False)
            ) is not None
            and p.connected
        )
        checks.append(
            Check(
                "and reconnects to a full graph",
                "every connection begins with a full Sync, so a restart needs no "
                "coordination and leaves nothing stale behind it",
                resynced,
                f"{len(store.snapshot().nodes)} node(s) after the restart",
            )
        )

    finally:
        await context.local_agent.stop()
        server.should_exit = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(serving, timeout=5.0)
        await context.collector.stop()
        await context.bus.aclose()
        await engine.stop()

    # -- and it cleans up after itself ------------------------------------

    checks.append(
        Check(
            "shutdown leaves nothing behind",
            "a socket left in the state directory is a path that outlives the process "
            "that could explain it, and the next start would find it in use",
            not context.local_agent.socket_path.exists(),
            str(context.local_agent.socket_path),
        )
    )

    checks.extend(await stopped_by_signal(agent_binary))

    return report("zero-config conformance", checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", type=Path, help="path to the agent binary")
    args = parser.parse_args(argv)

    if not args.agent.exists():
        print(f"no such agent binary: {args.agent}", file=sys.stderr)
        return 2

    return asyncio.run(run(args.agent.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
