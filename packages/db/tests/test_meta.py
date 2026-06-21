"""Per-collection protocol metadata (FR-E02-3)."""

from __future__ import annotations

from kantaq_db.meta import COLLECTION_META, collection_names
from kantaq_db.models import COLLECTION_MODELS

_EXPECTED = {
    "workspaces",
    "projects",
    "tickets",
    "comments",
    "ticket_relationships",
    "members",
    "tokens",
    "audit_events",
    "agent_proposals",
    "memory_entries",
    "memory_links",
    "devices",
    "capability_grants",
    "skill_containers",
    "skill_mappings",
    "conflict_records",
    "audit_anchors",
    "milestones",
    "ticket_milestones",
}
_VALID_MERGE = {"lww", "append_only", "authoritative_tx", "crdt"}
_VALID_AUTHORITY = {"local", "backend"}


def test_nineteen_collections_declared() -> None:
    assert set(collection_names()) == _EXPECTED
    assert len(collection_names()) == 19


def test_meta_matches_table_models() -> None:
    model_tables = {m.__tablename__ for m in COLLECTION_MODELS}  # type: ignore[attr-defined]
    assert model_tables == set(COLLECTION_META)


def test_every_collection_has_valid_policies() -> None:
    for name, meta in COLLECTION_META.items():
        assert meta.name == name
        assert meta.authority_mode in _VALID_AUTHORITY
        assert meta.merge_policy in _VALID_MERGE


def test_mvp_privacy_class_subset() -> None:
    # D-14: MVP uses team/plain/standard on every collection.
    for meta in COLLECTION_META.values():
        assert meta.privacy_class.visibility in {"local", "team"}
        assert meta.privacy_class.hosting_mode == "plain"
        assert meta.privacy_class.retention_policy == "standard"


def test_logs_are_append_only() -> None:
    assert COLLECTION_META["comments"].merge_policy == "append_only"
    assert COLLECTION_META["audit_events"].merge_policy == "append_only"
    # E07-T5: the Merkle anchor is append-only like the trail it commits to.
    assert COLLECTION_META["audit_anchors"].merge_policy == "append_only"


def test_tokens_never_optimistic() -> None:
    assert COLLECTION_META["tokens"].merge_policy == "authoritative_tx"


def test_grants_never_optimistic_devices_lww() -> None:
    # MOD-06: grants are authoritative_tx (never optimistically written);
    # device verify keys converge like ordinary collection rows.
    assert COLLECTION_META["capability_grants"].merge_policy == "authoritative_tx"
    assert COLLECTION_META["devices"].merge_policy == "lww"
