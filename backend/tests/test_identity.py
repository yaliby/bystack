"""Identity is the foundation everything else correlates on."""

from __future__ import annotations

import pytest

from bystack.core.identity import (
    URN,
    NodeKind,
    URNError,
    container_urn,
    host_urn,
    image_urn,
    service_urn,
)


def test_urn_roundtrip() -> None:
    urn = container_urn("engine1", "abc123")
    assert urn == "bystack:container:engine1/abc123"
    assert urn.kind == NodeKind.CONTAINER
    assert urn.segments == ("engine1", "abc123")


def test_urn_is_a_str_and_usable_as_a_dict_key() -> None:
    # The store keys every index by URN; if this ever stops holding, memory
    # and lookup cost both change character.
    urn = host_urn("engine1")
    assert {urn: 1}[URN("bystack:host:engine1")] == 1


@pytest.mark.parametrize(
    "value",
    ["", "bystack:host", "other:host:x", "bystack::x", "bystack:host:"],
)
def test_malformed_urns_are_rejected(value: str) -> None:
    with pytest.raises(URNError):
        URN(value)


def test_illegal_segment_characters_are_rejected_not_escaped() -> None:
    # Escaping would produce a stable-looking identity that is quietly wrong.
    # Failing loudly means a source we misunderstood surfaces immediately.
    with pytest.raises(URNError):
        container_urn("engine1", "abc/def")
    with pytest.raises(URNError):
        container_urn("engine:1", "abc")


def test_empty_segments_are_rejected() -> None:
    with pytest.raises(URNError):
        container_urn("engine1", "")


def test_image_identity_is_digest_based_and_reversible() -> None:
    urn = image_urn("sha256:abc123")
    assert urn.segments == ("sha256", "abc123")


def test_image_identity_is_host_independent() -> None:
    # The property that gives cross-host image correlation for free.
    assert image_urn("sha256:abc") == image_urn("sha256:abc")


def test_logical_and_physical_identities_are_distinct() -> None:
    # The whole point of the two-layer scheme: recreating a container mints a
    # new physical identity while the logical one is untouched, so history
    # and topology survive a redeploy.
    physical_before = container_urn("engine1", "old-container-id")
    physical_after = container_urn("engine1", "new-container-id")
    logical = service_urn("engine1", "shop", "web")

    assert physical_before != physical_after
    assert logical == service_urn("engine1", "shop", "web")


def test_legacy_colon_delimited_engine_ids_are_normalized() -> None:
    # Docker used a colon-delimited fingerprint before 25.0 and a UUID since.
    # A single old host in an otherwise modern fleet must not raise URNError
    # and take its whole provider down -- mixed-version fleets are the norm
    # in the home labs this targets.
    from bystack.core.identity import engine_scope

    legacy = engine_scope("TQ5X:PQVY:6JGP:VXQZ")
    modern = engine_scope("f952ee52-b480-42d6-bf22-cae4fddb142c")

    assert host_urn(legacy) == "bystack:host:TQ5XPQVY6JGPVXQZ"
    assert host_urn(modern) == "bystack:host:f952ee52-b480-42d6-bf22-cae4fddb142c"


def test_engine_id_normalization_is_stable_and_collision_free() -> None:
    from bystack.core.identity import engine_scope

    assert engine_scope("AB:CD") == engine_scope("AB:CD")
    assert engine_scope("AB:CD") != engine_scope("AB:CE")
