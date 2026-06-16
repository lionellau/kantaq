"""The RFC 6962 Merkle primitive (FR-E07-5): the structure, proofs, and defences.

Pinned against an **independent** RFC 6962 reference written in this module (the
D-11 spirit — a root produced and consumed by one helper proves nothing), a
checked-in golden root for version stability, and probed for the properties an
anchor lives on: every leaf has a verifying inclusion proof, and any tamper
(leaf, index, size, proof, root) is refused. Domain + leaf/interior prefixes are
asserted so the second-preimage defence and cross-digest separation can't regress.
"""

from __future__ import annotations

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kantaq_protocol import (
    AUDIT_MERKLE_DOMAIN,
    HASH_HEX,
    SchemaViolation,
    merkle_inclusion_proof,
    merkle_leaf_hash,
    merkle_root,
    verify_inclusion_proof,
)

# A fixed input → a fixed root: any change to the domain tag, the 0x00/0x01
# prefixes, the split, or the child order moves this and fails the build.
GOLDEN_LEAVES = [f"audit-row-{i}".encode() for i in range(7)]
GOLDEN_ROOT_7 = "6eae7c2febf7f4097703ab35776b64158be22d70e9f917b43c1bb1ba9dcd20b9"


# --------------------------------------------------------- independent twin


# Hardcoded, NOT imported (D-11): a reference that imports the domain would move
# in lock-step with a domain regression and catch nothing. The golden root below
# is the other half of the guard. We assert the module constant equals this so a
# domain change is still caught loudly.
_REF_DOMAIN = b"kantaq:audit-merkle:v1\x00"


def _ref_leaf(data: bytes) -> bytes:
    return hashlib.sha256(_REF_DOMAIN + b"\x00" + data).digest()


def _ref_node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_REF_DOMAIN + b"\x01" + left + right).digest()


def test_module_domain_matches_the_reference() -> None:
    assert AUDIT_MERKLE_DOMAIN == _REF_DOMAIN


def _ref_root(leaves: list[bytes]) -> str:
    """A hand-rolled RFC 6962 §2.1 MTH (raw bytes, recursive) — not the implementation."""
    hashed = [_ref_leaf(leaf) for leaf in leaves]

    def mth(items: list[bytes]) -> bytes:
        if len(items) == 1:
            return items[0]
        k = 1
        while k << 1 < len(items):
            k <<= 1
        return _ref_node(mth(items[:k]), mth(items[k:]))

    return mth(hashed).hex()


# --------------------------------------------------------- structure / vectors


def test_matches_an_independent_rfc6962_reference() -> None:
    for n in range(1, 17):
        leaves = [f"d{i}".encode() for i in range(n)]
        assert merkle_root(leaves) == _ref_root(leaves), f"root diverges at n={n}"


def test_golden_root_is_version_stable() -> None:
    assert merkle_root(GOLDEN_LEAVES) == GOLDEN_ROOT_7


def test_single_leaf_root_is_the_leaf_hash() -> None:
    """RFC 6962: MTH({d}) == leaf hash — no interior node, no duplication."""
    assert merkle_root([b"only"]) == merkle_leaf_hash(b"only")


def test_root_and_leaf_are_strict_lowercase_hex() -> None:
    assert HASH_HEX.match(merkle_root(GOLDEN_LEAVES)) is not None
    assert HASH_HEX.match(merkle_leaf_hash(b"x")) is not None


def test_empty_range_is_refused() -> None:
    with pytest.raises(SchemaViolation):
        merkle_root([])


def test_order_is_significant() -> None:
    assert merkle_root([b"a", b"b"]) != merkle_root([b"b", b"a"])


# --------------------------------------------------------- second-preimage / domain


def test_leaf_and_interior_prefixes_differ() -> None:
    """A 2-leaf root (an interior 0x01 node) can't equal a leaf (0x00) of the concatenation."""
    a, b = merkle_leaf_hash(b"a"), merkle_leaf_hash(b"b")
    root = merkle_root([b"a", b"b"])
    # the classic second-preimage: a forged leaf whose bytes are (leaf_a‖leaf_b)
    forged_leaf = merkle_leaf_hash(bytes.fromhex(a) + bytes.fromhex(b))
    assert root != forged_leaf


def test_domain_tag_separates_from_a_bare_merkle() -> None:
    bare = hashlib.sha256(b"\x00" + b"a").hexdigest()
    assert merkle_leaf_hash(b"a") != bare


# --------------------------------------------------------- inclusion proofs


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 13, 16])
def test_every_leaf_has_a_verifying_proof(n: int) -> None:
    leaves = [f"row-{i}".encode() for i in range(n)]
    root = merkle_root(leaves)
    for i in range(n):
        proof = merkle_inclusion_proof(leaves, i)
        assert verify_inclusion_proof(leaves[i], i, n, proof, root) is True


def test_proof_for_the_wrong_leaf_is_rejected() -> None:
    leaves = [f"row-{i}".encode() for i in range(7)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 3)
    assert verify_inclusion_proof(b"not-row-3", 3, 7, proof, root) is False


def test_proof_at_the_wrong_index_is_rejected() -> None:
    leaves = [f"row-{i}".encode() for i in range(7)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 3)
    assert verify_inclusion_proof(leaves[3], 4, 7, proof, root) is False


def test_a_tampered_proof_element_is_rejected() -> None:
    leaves = [f"row-{i}".encode() for i in range(7)]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 2)
    tampered = ["f" * 64, *proof[1:]] if proof else proof
    assert verify_inclusion_proof(leaves[2], 2, 7, tampered, root) is False


def test_proof_against_the_wrong_root_is_rejected() -> None:
    leaves = [f"row-{i}".encode() for i in range(7)]
    other = merkle_root([b"a", b"b", b"c"])
    proof = merkle_inclusion_proof(leaves, 0)
    assert verify_inclusion_proof(leaves[0], 0, 7, proof, other) is False


def test_verify_is_fail_closed_on_malformed_input() -> None:
    leaves = [b"a", b"b", b"c"]
    root = merkle_root(leaves)
    proof = merkle_inclusion_proof(leaves, 0)
    assert verify_inclusion_proof(leaves[0], -1, 3, proof, root) is False
    assert verify_inclusion_proof(leaves[0], 3, 3, proof, root) is False  # index >= size
    assert verify_inclusion_proof(leaves[0], 0, 3, proof, "NOTHEX") is False
    assert verify_inclusion_proof(leaves[0], 0, 3, ["zz" * 32], root) is False


def test_inclusion_proof_index_out_of_range_raises() -> None:
    with pytest.raises(SchemaViolation):
        merkle_inclusion_proof([b"a", b"b"], 5)


# --------------------------------------------------------- properties


@given(st.lists(st.binary(min_size=0, max_size=24), min_size=1, max_size=40))
def test_all_proofs_verify_for_any_tree(leaves: list[bytes]) -> None:
    root = merkle_root(leaves)
    n = len(leaves)
    for i in range(n):
        assert verify_inclusion_proof(leaves[i], i, n, merkle_inclusion_proof(leaves, i), root)


@given(
    st.lists(st.binary(min_size=1, max_size=16), min_size=2, max_size=24),
    st.integers(min_value=0),
)
def test_flipping_one_leaf_changes_the_root(leaves: list[bytes], pick: int) -> None:
    before = merkle_root(leaves)
    i = pick % len(leaves)
    mutated = list(leaves)
    mutated[i] = mutated[i] + b"\x01"  # any change
    assert merkle_root(mutated) != before
