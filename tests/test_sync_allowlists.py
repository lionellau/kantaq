"""The three sync allowlists can never drift again (MOD-04/MOD-05/MOD-02).

What syncs is declared in three places that previously drifted apart (the E13
memory collections reached the local applier but not the backend CHECK
constraint, so a team memory entry broke the whole push batch):

1. ``COLLECTION_META`` (MOD-02) — every collection's authority/merge policy;
2. ``SYNCABLE_MODELS`` (MOD-04, the local applier) — what a replica can fold;
3. ``ck_sync_events_collection`` (MOD-05, ``supabase/migrations/
   0002_sync_events.sql``) — what the backend accepts into the shared log.

This gate pins all three against each other, with the exclusions named and
justified, so adding a syncable collection in one place fails CI until the
other two (and the live-project ALTER note in ``supabase/README.md``) follow.
"""

from __future__ import annotations

import re
from pathlib import Path

from kantaq_db.meta import COLLECTION_META
from kantaq_sync_engine.apply import SYNCABLE_MODELS

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_EVENTS_SQL = REPO_ROOT / "supabase" / "migrations" / "0002_sync_events.sql"
SUPABASE_README = REPO_ROOT / "supabase" / "README.md"

# The collections deliberately OUTSIDE the sync surface, each with its reason.
# Changing this set is a protocol decision, not a refactor.
NEVER_SYNC: dict[str, str] = {
    # authority local + secret material (hashes) — never leaves the machine.
    "tokens": "MOD-06: local authority, secret material",
    # each replica's own local trail; replays write their own (source=sync).
    "audit_events": "MOD-07: per-replica trail",
    # devices/capability_grants joined the sync surface at E24-T7 (v0.2): see
    # test_trust_collections_join_the_surface_with_verified_ingestion below.
    # The E17 skill registry is db-backed but managed LOCALLY in v0.2
    # (architecture §6.1 "backend registry"); cross-replica sync is deferred,
    # so the CRUD service writes locally + audited and never emits.
    "skill_containers": "MOD-22: db-backed registry, off the sync allowlist in v0.2",
    "skill_mappings": "MOD-22: db-backed registry, off the sync allowlist in v0.2",
}


def _backend_allowlist() -> set[str]:
    """The CHECK constraint's collection set, parsed from the checked-in SQL."""
    sql = SYNC_EVENTS_SQL.read_text()
    match = re.search(
        r"ck_sync_events_collection\s+CHECK\s*\(collection\s+IN\s*\(([^)]*)\)",
        sql,
        flags=re.S,
    )
    assert match, "ck_sync_events_collection not found in 0002_sync_events.sql"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_local_applier_and_backend_constraint_agree() -> None:
    """The E13 regression: a collection the applier folds but the backend
    refuses breaks every push batch that contains one of its events."""
    assert set(SYNCABLE_MODELS) == _backend_allowlist()


def test_every_declared_collection_is_either_syncable_or_excluded_for_a_reason() -> None:
    declared = set(COLLECTION_META)
    syncable = set(SYNCABLE_MODELS)
    assert syncable <= declared, "the applier folds an undeclared collection"
    assert declared - syncable == set(NEVER_SYNC), (
        "a collection fell between the allowlists with no recorded reason — "
        "either register it for sync everywhere or add it to NEVER_SYNC with why"
    )


def test_syncable_collections_are_backend_authoritative() -> None:
    for name in SYNCABLE_MODELS:
        assert COLLECTION_META[name].authority_mode == "backend", (
            f"{name} syncs but is not backend-authoritative"
        )


def test_memory_collections_are_on_the_full_surface() -> None:
    """The specific E13 gap, pinned by name."""
    for name in ("memory_entries", "memory_links"):
        assert name in SYNCABLE_MODELS
        assert name in _backend_allowlist()


def test_trust_collections_join_the_surface_with_verified_ingestion() -> None:
    """E24-T7 (v0.2): the trust roots ingest now that the backend verifies
    signatures + grants (E24-T5 client + the E24-T6 atomic RPC). They are on
    the full surface (CHECK + applier) but route to the identity store on pull
    (DEBT-21), not the domain fold."""
    backend = _backend_allowlist()
    for name in ("devices", "capability_grants"):
        assert name in backend
        assert name in SYNCABLE_MODELS


def test_readme_alter_note_matches_the_constraint() -> None:
    """The live-project ALTER in supabase/README.md must list the same set."""
    readme = SUPABASE_README.read_text()
    match = re.search(
        r"ADD CONSTRAINT ck_sync_events_collection CHECK \(collection IN\s*\(([^)]*)\)",
        readme,
        flags=re.S,
    )
    assert match, "the README's existing-project ALTER note is missing"
    assert set(re.findall(r"'([a-z_]+)'", match.group(1))) == _backend_allowlist()
