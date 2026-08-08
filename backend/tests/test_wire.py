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
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bystack.agent.v1 import agent_pb2 as wire
from bystack.providers.agent import ingest

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
