"""RFC 6962-style Merkle anchors over audit ranges (E07-T5, FR-E07-5).

The Merkle half of the protocol core (MOD-17): the audit log (MOD-07) folds a
linear hash-chain range into one ``O(log n)`` anchor with this — the deferred
"transparency-log Merkle anchoring" the v0.1 hash chain's own docstring names.
It chains rows with ``hashing.chain_hash`` and signs events with the Ed25519
half; this is how a *range* proves itself with one root.

The construction is **RFC 6962 §2.1** (the Certificate-Transparency Merkle Tree
Hash), with one kantaq domain tag added:

    leaf:  MTH({d})    = SHA-256( DOMAIN ‖ 0x00 ‖ d )
    node:  MTH(D[0:n]) = SHA-256( DOMAIN ‖ 0x01 ‖ MTH(D[0:k]) ‖ MTH(D[k:n]) )
                         where k is the largest power of two strictly < n

- **Second-preimage separation (RFC 6962).** The ``0x00`` leaf prefix and
  ``0x01`` interior prefix mean a leaf digest can never be re-used as an
  interior digest — the standard defence against the Merkle second-preimage
  attack (presenting a leaf whose value equals an interior node).
- **Domain separation.** The ``kantaq:audit-merkle:v1`` tag (like
  ``AUDIT_CHAIN_DOMAIN`` / ``EVENT_SIGNING_DOMAIN`` / ``GRANT_SIGNING_DOMAIN``)
  means a kantaq audit-Merkle digest can never collide with an event, grant, or
  chain-link digest. We follow RFC 6962's *structure* and (honest-naming) claim
  **no** wire-compatibility with a public Certificate-Transparency log.
- **One codec.** Callers hash leaves they built with the same canonical RFC 8785
  codec every signature uses, so a root is byte-identical in SQLite and Postgres.
- **Strict hex.** A digest is 64 lowercase hex (``HASH_HEX``), like every other
  digest/signature/key in the protocol; an audit path is a list of such digests.

SHA-256 (FIPS 180-4) from the stdlib ``hashlib``; no Merkle/transparency-log
library clears the golden-rule bar (pymerkle 78★ GPL-3.0; pymerkletools 173★
unmaintained; Trillian/Rekor are Go *services*, not a Python primitive), so this
is built from scratch over the one codec — recorded in docs/stack.md.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from kantaq_protocol.errors import SchemaViolation
from kantaq_protocol.hashing import HASH_HEX

# Domain-separation tag (see hashing.AUDIT_CHAIN_DOMAIN). The RFC 6962 0x00/0x01
# leaf/interior prefixes ride *inside* this tag so the second-preimage defence is
# preserved while the whole scheme stays separated from every other kantaq digest.
AUDIT_MERKLE_DOMAIN = b"kantaq:audit-merkle:v1\x00"
_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def merkle_leaf_hash(leaf: bytes) -> str:
    """RFC 6962 leaf hash ``SHA-256(DOMAIN ‖ 0x00 ‖ leaf)`` → 64 lowercase hex."""
    if not isinstance(leaf, bytes | bytearray):
        raise SchemaViolation("a merkle leaf must be bytes")
    digest = hashlib.sha256()
    digest.update(AUDIT_MERKLE_DOMAIN)
    digest.update(_LEAF_PREFIX)
    digest.update(bytes(leaf))
    return digest.hexdigest()


def _node_hash(left: str, right: str) -> str:
    """RFC 6962 interior hash ``SHA-256(DOMAIN ‖ 0x01 ‖ left ‖ right)`` over two child digests."""
    for child in (left, right):
        if not isinstance(child, str) or HASH_HEX.match(child) is None:
            raise SchemaViolation("a merkle node child must be 64 lowercase hex characters")
    digest = hashlib.sha256()
    digest.update(AUDIT_MERKLE_DOMAIN)
    digest.update(_NODE_PREFIX)
    digest.update(bytes.fromhex(left))
    digest.update(bytes.fromhex(right))
    return digest.hexdigest()


def _split_point(n: int) -> int:
    """The largest power of two strictly less than ``n`` (RFC 6962 §2.1, ``n ≥ 2``)."""
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def _root_of(leaf_hashes: Sequence[str]) -> str:
    if len(leaf_hashes) == 1:
        return leaf_hashes[0]
    k = _split_point(len(leaf_hashes))
    return _node_hash(_root_of(leaf_hashes[:k]), _root_of(leaf_hashes[k:]))


def merkle_root(leaves: Sequence[bytes]) -> str:
    """The RFC 6962 Merkle Tree Hash over ``leaves`` (each leaf's raw bytes) → 64-hex.

    Raises ``SchemaViolation`` on an empty range — anchoring nothing is
    meaningless, so callers gate on a non-empty range (the audit summarize half
    refuses an unanchored range, not an empty one).
    """
    if len(leaves) == 0:
        raise SchemaViolation("cannot compute a merkle root over an empty range")
    return _root_of([merkle_leaf_hash(leaf) for leaf in leaves])


def merkle_inclusion_proof(leaves: Sequence[bytes], index: int) -> list[str]:
    """The RFC 6962 §2.1.1 audit path (sibling digests, hex) proving ``leaves[index]``.

    The path is bottom-up; the verifier re-derives each sibling's left/right
    position from ``(index, tree_size)`` (RFC 6962), so no orientation flags are
    carried. Raises ``SchemaViolation`` if ``index`` is out of range.
    """
    n = len(leaves)
    if not 0 <= index < n:
        raise SchemaViolation(f"merkle proof index {index} out of range for {n} leaves")
    return _path([merkle_leaf_hash(leaf) for leaf in leaves], index)


def _path(leaf_hashes: Sequence[str], index: int) -> list[str]:
    n = len(leaf_hashes)
    if n == 1:
        return []
    k = _split_point(n)
    if index < k:
        return [*_path(leaf_hashes[:k], index), _root_of(leaf_hashes[k:])]
    return [*_path(leaf_hashes[k:], index - k), _root_of(leaf_hashes[:k])]


def verify_inclusion_proof(
    leaf: bytes,
    index: int,
    tree_size: int,
    proof: Sequence[str],
    root: str,
) -> bool:
    """Verify an RFC 6962 §2.1.1 audit path — fail-closed, never raises.

    Returns ``True`` only when ``leaf`` at ``index`` in a tree of ``tree_size``
    leaves, combined with ``proof``, reproduces ``root``. Any malformed input
    (out-of-range index, non-hex proof element or root, a path of the wrong
    length) returns ``False`` rather than raising — a verifier refuses, it does
    not crash on hostile input.
    """
    if not 0 <= index < tree_size:
        return False
    if not isinstance(root, str) or HASH_HEX.match(root) is None:
        return False
    for sibling in proof:
        if not isinstance(sibling, str) or HASH_HEX.match(sibling) is None:
            return False
    # RFC 6962 §2.1.1 path verification.
    fn, sn = index, tree_size - 1
    computed = merkle_leaf_hash(leaf)
    for sibling in proof:
        if sn == 0:
            return False  # path longer than the tree is deep
        if (fn & 1) or fn == sn:
            computed = _node_hash(sibling, computed)
            if not (fn & 1):
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            computed = _node_hash(computed, sibling)
        fn >>= 1
        sn >>= 1
    return sn == 0 and computed == root
