"""E05-T2.2: detect_merge matches the golden conflict_vectors (MOD-26 §B3).

The same vectors pin the plpgsql RPC on EphemeralPostgres (T2.6) — one decision,
one truth. Every B3 branch is a row: R0 apply (saw head), R1 auto-merge (different
field), R2 idempotent (same value), R3 conflict (different value), R4 multi-write
collapse to field-head, R5 genesis (base_rev null), R6 edit-vs-delete, R7
delete-idempotent, plus the (B,H]-boundary (S1) and null-vs-value (S2) sub-claims.
"""

from __future__ import annotations

import pytest

from kantaq_sync_engine import conflict_record_id, detect_merge
from kantaq_test_harness.vectors import ConflictVector, load_conflict_vectors

_VECTORS = load_conflict_vectors()


@pytest.mark.parametrize("vector", _VECTORS, ids=lambda v: v.name)
def test_detect_merge_matches_golden_vector(vector: ConflictVector) -> None:
    outcome = detect_merge(vector.committed_prefix, vector.incoming)

    assert outcome.entity_verdict == vector.expected_verdict, vector.name
    assert len(outcome.conflicts) == len(vector.expected_conflicts), vector.name
    for got, want in zip(outcome.conflicts, vector.expected_conflicts, strict=True):
        assert got.field == want["field"]
        assert got.contending_revision == want["contending_revision"]
        assert got.head_value == want["head_value"]
        assert got.incoming_value == want["incoming_value"]
        # The id is deterministic and re-derivable from the cited revisions alone.
        assert got.conflict_record_id == conflict_record_id(
            vector.incoming.event.entity_id,
            want["field"],
            [vector.incoming.revision, want["contending_revision"]],
        )


def test_conflict_id_is_stable_and_well_separated() -> None:
    same = conflict_record_id("tkt_x", "status", [7, 5])
    assert same == conflict_record_id("tkt_x", "status", [5, 7])  # sorted internally
    assert len(same) == 26  # fits CollectionBase.id
    assert same != conflict_record_id("tkt_x", "priority", [7, 5])  # field-separated
    assert same != conflict_record_id("tkt_y", "status", [7, 5])  # entity-separated
    assert same != conflict_record_id("tkt_x", "status", [7, 6])  # revision-bound


def test_every_b3_branch_is_present_in_the_vectors() -> None:
    # Guard against the golden file silently shrinking below the B3 branch set.
    names = {v.name for v in _VECTORS}
    for branch in ("R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7"):
        assert any(n.startswith(branch + "_") for n in names), f"missing branch {branch}"
