"""The wire contract, and the two ways it can silently rot.

Both failures here are invisible at runtime, which is why they get tests
rather than review:

1. **The checked-in bindings drift from the `.proto`.** The generated code is
   committed so that installing the Controller never needs a protobuf
   compiler. That convenience is only safe if something notices when someone
   edits the schema and forgets to regenerate.

2. **The mapper reads a field the agent does not send.** `mapper.py` consumes
   Docker's vocabulary, and under the agent model that vocabulary is rebuilt
   from wire messages. Add `payload.get("HostConfig")` to the mapper and
   nothing breaks, nothing warns — the attribute is simply always `None` in
   the UI, on every agent-backed host, forever. `docs/MIGRATION.md` §5 asks
   for exactly this check.

3. **A number the Controller believes drifts from the one the agent
   enforces.** Neither side imports the other, so a handful of constants are
   written down twice. Lower `MAX_LOG_LINES` in the agent and the route goes
   on advertising a bound it no longer has, accepting a request the agent
   quietly truncates. This repo's rule is that cross-language duplication
   gets a guard rather than a comment; the third test is that guard.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bystack.agent.v1 import agent_pb2 as wire
from bystack.conformance import runner
from bystack.infra import releases
from bystack.providers.agent import commands, ingest, upgrade

BACKEND = Path(__file__).resolve().parent.parent
GENERATOR = BACKEND / "scripts" / "generate_proto.py"

#: Docker's JSON uses PascalCase keys; ours are lowercase. Matching on the
#: first character separates "a field the mapper reads off a Docker payload"
#: from "an attribute the mapper writes into a node", with no list to keep.
_DOCKER_KEY = re.compile(r"""(?:\.get\(|\[)["']([A-Z][A-Za-z]*)["']""")


def test_the_committed_bindings_match_the_proto() -> None:
    """Fails when the schema was edited without regenerating.

    Skipped rather than failed when `grpcio-tools` is absent: it is a dev
    dependency, and a contributor running the suite without it should see the
    other 200 tests, not one red herring.
    """
    pytest.importorskip("grpc_tools", reason="grpcio-tools is a dev dependency")

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=BACKEND,
    )

    assert result.returncode == 0, result.stderr


def test_every_docker_field_the_mapper_reads_is_carried_on_the_wire() -> None:
    """The drift guard from MIGRATION §5.

    Not a comparison of names -- the `.proto` is snake_case and Docker is
    PascalCase, and translating between them is precisely `ingest`'s job. It
    compares what the mapper *reads* against what `ingest` actually *produces*
    from a fully-populated set of wire messages. A field added to the mapper
    with no corresponding wire field fails here, at the seam, rather than
    manifesting as a permanently-null attribute in production.
    """
    read = _keys_read_by_mapper()
    produced = _keys_produced_by_ingest()

    missing = read - produced
    assert not missing, (
        f"mapper.py reads Docker fields the agent never sends: {sorted(missing)}. "
        f"Add them to proto/bystack/agent/v1/agent.proto and to providers/agent/ingest.py."
    )


def test_the_constants_mirrored_across_languages_still_agree() -> None:
    """Every number this tree writes down twice, checked against the copy.

    The agent is the authority in both cases and deliberately so: it is the
    side holding the memory budget and the round-trip cost, and it must not
    trust a number the Controller sent it. The Python copies exist so the
    refusal happens before a frame crosses the network and so the OpenAPI
    schema can state the bound — which is worth having exactly as long as it
    is the same number.

    A regex over the Rust source rather than a generated header: two integers
    do not justify a build step, and this is the same shape as
    `_keys_read_by_mapper` above.
    """
    assert _rust_const("docker.rs", "MAX_LOG_LINES") == commands.MAX_LOG_TAIL, (
        "the agent's log clamp and the Controller's differ; the route would "
        "accept a tail the agent silently truncates"
    )
    inspects = _rust_const("informer.rs", "MAX_CRASH_LOOP_INSPECTS")
    assert inspects == runner.MAX_CRASH_LOOP_INSPECTS, (
        "the agent's inspect ceiling moved; the conformance check that asserts "
        "it would be asserting the old number"
    )
    assert _rust_const("procfs.rs", "MAX_INVENTORY_ITEMS") == commands.MAX_INVENTORY, (
        "the agent's inventory cap and the Controller's differ; the picker "
        "would advertise a limit the agent silently truncates, and an operator "
        "would read a short list as the whole machine"
    )


def test_the_manifest_vocabulary_agrees_across_languages() -> None:
    """The signed document is parsed by both sides, strictly, by hand.

    Not generated from anything and not derivable: it is a text format written
    twice, once in Rust and once in Python, because the two readers must agree
    on which bytes are acceptable. The drift this catches is the expensive
    kind and it is silent -- a Controller that indexes a release every host
    refuses, or worse, one that skips a field an agent enforces.
    """
    assert _rust_string("upgrade.rs", "MAGIC") == releases.MAGIC, (
        "the manifest format string differs; the Controller would offer a "
        "release every agent in the fleet refuses to read"
    )
    assert _rust_string("upgrade.rs", "ARTIFACT_NAME") == releases.ARTIFACT, (
        "what is being signed is spelled differently on the two sides"
    )
    # The capability, which is what stops a two-megabyte transfer to a host
    # that was always going to refuse it.
    agent_source = (BACKEND.parent / "agent" / "src" / "session.rs").read_text()
    assert f'"{upgrade.CAP_UPGRADE}"' in agent_source, (
        "the agent no longer advertises the capability the Controller checks; "
        "every push would be refused with no diagnosis"
    )


def test_every_host_field_the_mapper_reads_is_carried_on_the_wire() -> None:
    """The §5 drift guard again, for systemd and /proc.

    Same failure it exists to catch and a worse version of it: a Docker field
    the agent stops sending shows up as one null attribute, while a unit field
    that goes missing shows up as a *state* -- `active_state` silently empty
    reads as a service nobody can say anything about, on a card an operator is
    looking at during an incident.

    Compared against the mapper's own key set rather than a hand-written list,
    so adding `Fragment.Path` to `providers/host/mapper.py` and forgetting the
    `.proto` fails here rather than in front of somebody.
    """
    source = (BACKEND / "src" / "bystack" / "providers" / "host" / "mapper.py").read_text()
    read = set(_DOCKER_KEY.findall(source))

    produced: set[str] = set()
    _collect(ingest._unit(_full_unit()), produced)
    _collect(ingest._process(_full_process()), produced)

    missing = read - produced
    assert not missing, (
        f"providers/host/mapper.py reads fields the agent never sends: {sorted(missing)}. "
        f"Add them to proto/bystack/agent/v1/agent.proto and to providers/agent/ingest.py."
    )


def _full_unit() -> wire.Unit:
    """Every field set, so a field ingest forgets to translate is visible."""
    return wire.Unit(
        name="nginx.service",
        description="A high performance web server",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        main_pid=4242,
        active_enter_timestamp=1_700_000_000_000_000,
        n_restarts=3,
        result="exit-code",
        exec_main_status=137,
        fragment_path="/usr/lib/systemd/system/nginx.service",
    )


def _full_process() -> wire.Process:
    return wire.Process(
        watch_id="w1",
        match_kind="cmdline",
        pattern="worker.py",
        total=2,
        instances=[
            wire.ProcessInstance(
                pid=4242,
                comm="python3",
                cmdline="python3 worker.py --queue=default",
                state="S",
                started_at=1_700_000_000,
                uid=1000,
                cgroup="0::/system.slice/worker.service",
            )
        ],
    )


def test_the_installer_looks_for_the_files_the_agent_writes() -> None:
    """`install-agent.sh` decides "enrolled" by looking in the state directory.

    Three names, written by the agent and read by a shell script, with nothing
    between them. The drift is silent in the worst direction: the installer
    concludes the host never enrolled, says so, exits non-zero -- and leaves
    the single-use join token in `agent.env`. A token beside a stored
    certificate makes the agent **re-enrol on its next start** (`main.rs`,
    `ensure_enrolled`, deliberately), and the second redemption of a
    single-use token is refused.

    So the host survives exactly until something restarts it. ADR-0017's
    upgrade always does, which turns a push into a host that drops off the
    map -- and the rollback puts the previous binary back into the same wall.
    """
    installer = (BACKEND.parent / "scripts" / "install-agent.sh").read_text()
    for name in ("CERT_FILE", "KEY_FILE", "CA_FILE"):
        written = _rust_string("trust.rs", name)
        assert written in installer, (
            f"the agent writes {written} ({name} in agent/src/trust.rs) and "
            f"install-agent.sh does not look for it; it would report every "
            f"successful enrolment as a failure and leave the spent token in "
            f"agent.env, which takes the host down on its next restart"
        )


def _rust_string(filename: str, name: str) -> str:
    """The string literal bound to ``name`` in one of the agent's sources."""
    source = (BACKEND.parent / "agent" / "src" / filename).read_text()
    found = re.search(rf'\b{name}\s*:\s*&str\s*=\s*"([^"]*)"', source)
    assert found is not None, f"{name} is no longer defined in agent/src/{filename}"
    return found.group(1)


def _rust_const(filename: str, name: str) -> int:
    """The integer literal bound to ``name`` in one of the agent's sources."""
    source = (BACKEND.parent / "agent" / "src" / filename).read_text()
    found = re.search(rf"\b{name}\s*:\s*\w+\s*=\s*(\d[\d_]*)", source)
    assert found is not None, f"{name} is no longer defined in agent/src/{filename}"
    return int(found.group(1).replace("_", ""))


def _keys_read_by_mapper() -> set[str]:
    source = (BACKEND / "src" / "bystack" / "providers" / "docker" / "mapper.py").read_text()
    return set(_DOCKER_KEY.findall(source))


def _keys_produced_by_ingest() -> set[str]:
    """Every key, at any depth, that ingest builds from a full wire payload."""
    payloads = [
        ingest._container(_full_container()),
        ingest._network(_full_network()),
        ingest._volume(_full_volume()),
        ingest._image(_full_image()),
        ingest._engine_info(_full_engine(), "engine"),
    ]
    found: set[str] = set()
    for payload in payloads:
        _collect(payload, found)
    return found


def _collect(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            found.add(key)
            _collect(nested, found)
    elif isinstance(value, list):
        for item in value:
            _collect(item, found)


# --------------------------------------------------------------------------
# Fully-populated messages.
#
# Every field set, including the ones a real agent would often leave empty:
# the point is to enumerate what the schema *can* carry, and a field left at
# its proto3 default would still appear as a key, but building them out keeps
# the fixtures readable as examples.
# --------------------------------------------------------------------------


def _full_container() -> wire.Container:
    return wire.Container(
        id="c1",
        names=["/web"],
        image="nginx:latest",
        image_id="sha256:abc",
        command="nginx",
        created=1,
        state="running",
        status_text="Up 3 hours",
        health="healthy",
        restart_count=417,
        labels={"com.docker.compose.project": "shop"},
        ports=[wire.Port(private_port=80, public_port=8080, protocol="tcp", host_ip="0.0.0.0")],
        networks=[
            wire.NetworkAttachment(network_id="n1", name="bridge", ipv4="1.2.3.4", aliases=["a"])
        ],
        mounts=[wire.Mount(type="volume", name="v", destination="/d", mode="rw", rw=True)],
    )


def _full_network() -> wire.Network:
    return wire.Network(
        id="n1", name="shop_default", driver="bridge", scope="local",
        internal=True, attachable=True, ingress=True,
        subnets=["172.17.0.0/16"], labels={"a": "b"},
    )


def _full_volume() -> wire.Volume:
    return wire.Volume(
        name="v", driver="local", mountpoint="/m", scope="local",
        created_at="2026-01-01T00:00:00Z", labels={"a": "b"},
    )


def _full_image() -> wire.Image:
    return wire.Image(
        id="sha256:abc", repo_tags=["nginx:latest"], repo_digests=["nginx@sha256:d"],
        size=1, created=2, labels={"a": "b"},
    )


def _full_engine() -> wire.EngineInfo:
    return wire.EngineInfo(
        id="e1", name="lab", server_version="29.6.0", operating_system="Fedora",
        kernel_version="6.19", architecture="x86_64", ncpu=8, mem_total=16,
        containers_running=2, containers_total=3,
    )


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_the_envelope_carries_exactly_one_payload() -> None:
    envelope = wire.Envelope(hello=wire.Hello(engine_id="e1"))

    assert envelope.WhichOneof("payload") == "hello"


def test_an_empty_envelope_names_no_payload() -> None:
    """What an unknown frame from a newer agent decodes to on an older
    Controller. It must be inspectable rather than raising, because mixed
    versions are a normal operating state under ADR-0008."""
    assert wire.Envelope().WhichOneof("payload") is None


def test_an_authoritative_delta_costs_a_fraction_of_the_payloads() -> None:
    """The measurement the whole protocol is designed around.

    ADR-0009's table: 100 containers with one restarted is ~180 KB of
    payloads against a ~7 KB authoritative delta. Asserted against a
    realistically-sized container rather than a minimal fixture, because the
    entire argument for sending the id set on every frame rests on ids being
    cheap *relative to payloads* -- and a lean fixture would flatter it.

    If this ratio ever collapses, the design has lost its reason to exist and
    should be reconsidered rather than quietly kept.
    """
    entities = [
        wire.Entity(id=f"{index:064x}", container=_realistic_container(index))
        for index in range(100)
    ]

    every_payload = wire.Envelope(
        sync=wire.Sync(slice=wire.SLICE_CONTAINER, entities=entities)
    ).ByteSize()
    authoritative = wire.Envelope(
        delta=wire.Delta(
            slice=wire.SLICE_CONTAINER,
            ids=[entity.id for entity in entities],
            changed=[entities[0]],
        )
    ).ByteSize()

    assert every_payload > 100 * 1024
    assert authoritative < 12 * 1024
    assert authoritative * 10 < every_payload


def test_a_steady_state_delta_is_ids_and_nothing_else() -> None:
    """Nothing changed, so no payload is sent.

    This is the frame a healthy host emits after a coalesced burst that turned
    out to change nothing, and the Controller reconciles it in full at this
    cost. An idle host sends no frame at all.
    """
    frame = wire.Envelope(
        delta=wire.Delta(
            slice=wire.SLICE_CONTAINER, ids=[f"{index:064x}" for index in range(100)]
        )
    )

    assert frame.ByteSize() < 8 * 1024


def _realistic_container(index: int) -> wire.Container:
    """A compose-managed container as actually observed, not as fixtured.

    The labels are what dominates: compose writes its whole model into them,
    and a real image adds OCI annotations on top. This is why a payload is
    ~1.8 KB and an id is 64 bytes.
    """
    return wire.Container(
        id=f"{index:064x}",
        names=[f"/shop-web-{index}"],
        image="ghcr.io/example/shop-web:2026.4.1",
        image_id="sha256:" + "a" * 64,
        command="/docker-entrypoint.sh nginx -g 'daemon off;'",
        created=1_700_000_000,
        state="running",
        status_text="Up 3 hours (healthy)",
        labels={
            "com.docker.compose.project": "shop",
            "com.docker.compose.service": "web",
            "com.docker.compose.container-number": str(index),
            "com.docker.compose.project.working_dir": "/srv/deploy/shop",
            "com.docker.compose.project.config_files": "/srv/deploy/shop/compose.yaml",
            "com.docker.compose.depends_on": "db:service_healthy:true,cache:service_started:false",
            "com.docker.compose.oneoff": "False",
            "com.docker.compose.version": "2.31.0",
            "org.opencontainers.image.source": "https://github.com/example/shop",
            "org.opencontainers.image.revision": "b" * 40,
            "org.opencontainers.image.created": "2026-04-01T09:12:33Z",
            "org.opencontainers.image.licenses": "Apache-2.0",
        },
        ports=[
            wire.Port(private_port=80, public_port=8080, protocol="tcp", host_ip="0.0.0.0"),
            wire.Port(private_port=80, public_port=8080, protocol="tcp", host_ip="::"),
        ],
        networks=[
            wire.NetworkAttachment(
                network_id="c" * 64, name="shop_default", ipv4="172.24.0.5",
                aliases=["web", f"shop-web-{index}"],
            )
        ],
        mounts=[
            wire.Mount(type="volume", name="shop_data", destination="/var/lib/data",
                       mode="rw", rw=True),
            wire.Mount(type="bind", name="", destination="/etc/localtime",
                       mode="ro", rw=False),
        ],
    )


def test_every_slice_maps_to_graph_kinds() -> None:
    """A slice with no kinds would reconcile nothing and silently ignore an
    entire class of entity."""
    declared = {
        value.number
        for value in wire.Slice.DESCRIPTOR.values
        if value.number != wire.SLICE_UNSPECIFIED
    }

    assert declared == set(ingest.SLICE_KINDS)
