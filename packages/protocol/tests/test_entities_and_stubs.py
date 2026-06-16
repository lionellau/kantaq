"""Entity types, structured errors, and the crdt stub (FR-E03-1/5/7)."""

from __future__ import annotations

import pytest

from kantaq_protocol import (
    Actor,
    AuditAnchor,
    BlobRef,
    Collection,
    PolicyDenied,
    ProtocolError,
    SchemaViolation,
    Snapshot,
    StaleBaseRev,
    TeamManifest,
    UnknownCollection,
    crdt,
)


def test_the_structured_error_codes_are_the_wire_vocabulary() -> None:
    assert StaleBaseRev("x").code == "stale_base_rev"
    assert PolicyDenied("x").code == "policy_denied"
    assert SchemaViolation("x").code == "schema_violation"
    assert UnknownCollection("x").code == "unknown_collection"


def test_every_reject_is_a_protocol_error_with_a_message() -> None:
    for exc_type in (StaleBaseRev, PolicyDenied, SchemaViolation, UnknownCollection):
        error = exc_type("why it was refused")
        assert isinstance(error, ProtocolError)
        assert error.message == "why it was refused"
        with pytest.raises(ProtocolError):
            raise error


def test_entities_are_frozen_values() -> None:
    actor = Actor(actor_id="a", kind="device", public_key="ab" * 32)
    with pytest.raises(AttributeError):
        actor.actor_id = "b"  # type: ignore[misc]


def test_the_manifest_composes_actors_and_collections() -> None:
    manifest = TeamManifest(
        team_id="t1",
        name="kantaq",
        actors=(Actor(actor_id="a", kind="human"),),
        collections=(Collection(name="tickets", authority_mode="backend", merge_policy="lww"),),
    )
    assert manifest.collections[0].merge_policy == "lww"
    assert manifest.actors[0].public_key is None


def test_snapshot_blobref_and_anchor_shapes() -> None:
    snapshot = Snapshot(collection="tickets", as_of_rev=42, entities={"t1": {"title": "x"}})
    blob = BlobRef(
        blob_id="b1", filename="a.png", media_type="image/png", size_bytes=1, sha256="00"
    )
    anchor = AuditAnchor(
        anchor_id="an1",
        range_start="e1",
        range_end="e9",
        root="ff",
        tree_size=9,
        chain_tip="aa",
    )
    assert snapshot.as_of_rev == 42
    assert blob.size_bytes == 1
    assert anchor.root == "ff"
    assert anchor.tree_size == 9
    assert anchor.external_pin is None


def test_crdt_merge_is_an_honest_stub() -> None:
    assert crdt.merge() == crdt.POLICY_NOT_IMPLEMENTED
    assert crdt.merge({"a": 1}, {"a": 2}, {"a": 3}) == "policy_not_implemented"
