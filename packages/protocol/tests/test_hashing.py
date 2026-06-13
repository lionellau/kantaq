"""The audit hash-chain primitive (FR-E07-4): deterministic, domain-separated.

The construction is pinned against an **independent** stdlib reference computed
in this module (the D-11 spirit: a digest produced and consumed by the same
helper proves nothing), and probed for the sensitivity a hash chain lives on —
one changed field, one changed predecessor, the genesis boundary.
"""

from __future__ import annotations

import hashlib

import pytest

from kantaq_protocol import (
    AUDIT_CHAIN_DOMAIN,
    HASH_HEX,
    SchemaViolation,
    canonicalize,
    chain_hash,
)

GENESIS = None
A_HASH = "a" * 64
RECORD = {"id": "01JAUDIT0000000000000001", "action": "ticket.update", "actor_seq": 3}


def _reference(previous: str | None, record: dict[str, object]) -> str:
    """Hand-rolled twin of the chain construction — not the implementation."""
    return hashlib.sha256(
        AUDIT_CHAIN_DOMAIN + (previous or "").encode("ascii") + b"\x00" + canonicalize(record)
    ).hexdigest()


def test_matches_an_independent_reference() -> None:
    assert chain_hash(A_HASH, RECORD) == _reference(A_HASH, RECORD)
    assert chain_hash(GENESIS, RECORD) == _reference(GENESIS, RECORD)


def test_output_is_strict_lowercase_hex() -> None:
    digest = chain_hash(GENESIS, RECORD)
    assert HASH_HEX.match(digest) is not None
    assert len(digest) == 64


def test_deterministic_for_equal_inputs() -> None:
    assert chain_hash(A_HASH, RECORD) == chain_hash(A_HASH, dict(RECORD))


def test_key_order_does_not_change_the_digest() -> None:
    """The canonical codec sorts keys, so record field order is irrelevant."""
    reordered = {"actor_seq": 3, "action": "ticket.update", "id": "01JAUDIT0000000000000001"}
    assert chain_hash(A_HASH, reordered) == chain_hash(A_HASH, RECORD)


def test_genesis_differs_from_a_zero_predecessor() -> None:
    """A None predecessor (genesis) is not the same statement as some hash."""
    assert chain_hash(GENESIS, RECORD) != chain_hash("0" * 64, RECORD)


def test_one_changed_field_changes_the_digest() -> None:
    tampered = {**RECORD, "action": "ticket.delete"}
    assert chain_hash(A_HASH, tampered) != chain_hash(A_HASH, RECORD)


def test_a_different_predecessor_changes_the_digest() -> None:
    assert chain_hash("b" * 64, RECORD) != chain_hash(A_HASH, RECORD)


def test_predecessor_must_be_a_valid_digest_spelling() -> None:
    for bad in ("A" * 64, "abc", "g" * 64, " " + "a" * 63, A_HASH + "a"):
        with pytest.raises(SchemaViolation, match="previous chain hash"):
            chain_hash(bad, RECORD)


def test_record_must_be_canonically_encodable() -> None:
    # Floats are outside the restricted RFC 8785 profile the codec enforces.
    with pytest.raises(SchemaViolation):
        chain_hash(GENESIS, {"id": "01JAUDIT0000000000000001", "confidence": 0.5})


def test_record_must_be_a_mapping() -> None:
    with pytest.raises(SchemaViolation, match="object"):
        chain_hash(GENESIS, ["not", "a", "mapping"])  # type: ignore[arg-type]
