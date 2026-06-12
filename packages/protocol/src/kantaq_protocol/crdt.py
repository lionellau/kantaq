"""The ``crdt`` merge-policy stub (FR-E03-7).

The merge-policy vocabulary (architecture §8.1) reserves ``crdt`` for
post-v0.1 collections; nothing in MVP declares it. The stub exists so the
policy name is dispatchable today and callers get the documented structured
answer instead of an AttributeError when it is reached.
"""

from __future__ import annotations

from typing import Any

POLICY_NOT_IMPLEMENTED = "policy_not_implemented"


def merge(base: Any = None, ours: Any = None, theirs: Any = None) -> str:
    """Always ``policy_not_implemented`` — no CRDT semantics exist yet."""
    return POLICY_NOT_IMPLEMENTED
