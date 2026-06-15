"""The conflict-merge decision (MOD-26 §B3) — the shared §8.1 reference.

This is the one place the per-field merge rule lives in Python: the client
preview, the MOD-28 self-host backend, and the cross-check against the Supabase
plpgsql RPC all run THIS function. The RPC mirrors it (returning the raw
contender tuple, never re-hashing); both are pinned against the golden
``conflict_vectors.json`` — the ``test_verb_map_parity`` discipline applied to
merge, so "one decision, one truth" cannot drift.

**Precondition (the gappy-prefix hazard, design-review P1).** ``committed_prefix``
must be the entity's COMPLETE committed history with ``revision <
incoming.revision``. The caller guarantees it: production detection runs at the
RPC, which holds the gapless prefix in the same transaction that assigns
``incoming``'s revision; a replica computing a preview must be caught up. This
function is pure over the prefix it is given — it cannot see a gap it does not
hold, so the deterministic id is replica-independent only on a gapless prefix.

**Equality (design-review P2).** Field values are compared through the one
canonical codec (``canonicalize``), the same bytes signatures commit to — never
raw ``==`` — so ``1`` vs ``1.0`` / null ambiguity and SQLite-vs-Postgres JSON
number normalization can never split "idempotent" from "conflict" within or
across stores. (The codec refuses floats outright, which is the correct
fail-loud, not a silent mis-compare.)

``rebase_required`` for a stale agent proposal is deliberately NOT here: it is a
decision over the *target ticket's* head, not a single-entity merge over the
proposal, so it lives in the resolve/proposal path, not ``detect_merge``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from kantaq_core.tracker.events import REVIVE_FIELD
from kantaq_db.ids import encode_base32
from kantaq_protocol import canonicalize
from kantaq_sync_engine.events import CommittedEvent

# Domain-separation tag (hashing.py precedent) so a conflict id can never collide
# with an audit-link or a signing message.
CONFLICT_ID_DOMAIN = b"kantaq:conflict-id:v1\x00"
# The sentinel "field" for an edit-vs-delete conflict (the whole entity, not a
# scalar). Hashed as a literal string in the id; never a real column name.
ENTITY_FIELD = "__entity__"
_ID_BITS = 130  # 5 bits/char × 26 chars = CollectionBase.id capacity

FieldVerdict = Literal["apply", "auto_merge", "idempotent", "conflict"]
EntityVerdict = Literal[
    "apply",  # B == H: the write saw the latest head
    "auto_merge",  # B < H but no field contends (different scalars)
    "idempotent",  # every set field already holds the incoming value
    "conflict",  # at least one field contends
    "edit_vs_delete",  # a patch that predates a committed tombstone it never saw
    "delete_idempotent",  # a tombstone over an already-deleted entity
]


def conflict_record_id(entity_id: str, field: str, contending_revisions: Sequence[int]) -> str:
    """The deterministic, forgery-resistant conflict id (MOD-26 §B4).

    Bound to the two **immutable server-assigned revisions** (the loser's and the
    field-head's), never raw ``event_id``s — so any replica holding the committed
    prefix re-derives the same id (cross-replica insert-once) and a pulling
    replica re-verifies the cited revisions actually collide before accepting a
    record. Domain-separated SHA-256, truncated to 130 bits and Crockford-encoded
    to 26 chars to fit ``CollectionBase.id``. Hashed in EXACTLY ONE language: the
    RPC returns the raw tuple and never re-hashes, so there is no cross-language
    id-drift surface.
    """
    revs = ":".join(str(int(r)) for r in sorted(contending_revisions))
    canonical = f"{entity_id}\x1f{field}\x1f{revs}".encode()
    digest = sha256(CONFLICT_ID_DOMAIN + canonical).digest()
    value = int.from_bytes(digest[:17], "big") & ((1 << _ID_BITS) - 1)
    return encode_base32(value, 26)


def _canon_eq(a: Any, b: Any) -> bool:
    """Equality through the canonical codec (the one signatures commit to)."""
    return canonicalize(a) == canonicalize(b)


@dataclass(frozen=True)
class FieldDecision:
    """The per-field outcome (B3). ``contending_revision``/``conflict_record_id``
    are set only when ``verdict == 'conflict'`` (or the field-head for an
    idempotent re-set)."""

    field: str
    verdict: FieldVerdict
    contending_revision: int | None
    head_value: Any
    incoming_value: Any
    conflict_record_id: str | None


@dataclass(frozen=True)
class MergeOutcome:
    entity_verdict: EntityVerdict
    field_decisions: tuple[FieldDecision, ...]
    head_rev: int

    @property
    def conflicts(self) -> tuple[FieldDecision, ...]:
        """The field decisions that minted a conflict_record."""
        return tuple(d for d in self.field_decisions if d.verdict == "conflict")


def _field_head(window: Sequence[CommittedEvent], field: str) -> CommittedEvent | None:
    """The last committed setter of ``field`` in the window, by revision (B3:51:
    multi-writes collapse to E-vs-field-head, so the contender is replica-
    independent on a gapless prefix)."""
    setters = [ce for ce in window if ce.event.op != "tombstone" and field in ce.event.payload]
    return max(setters, key=lambda ce: ce.revision, default=None)


def detect_merge(
    committed_prefix: Sequence[CommittedEvent], incoming: CommittedEvent
) -> MergeOutcome:
    """Decide the per-field merge of ``incoming`` against the committed prefix.

    ``incoming`` carries its assigned commit ``revision`` (the RPC assigns it in
    the same transaction, before this check). ``base`` = ``incoming.base_rev`` or
    0 (genesis floor — a ``None`` base never silently LWW-overwrites). The
    contention window is ``(base, head_rev]``.
    """
    head_rev = max((ce.revision for ce in committed_prefix), default=0)
    base = incoming.event.base_rev if incoming.event.base_rev is not None else 0
    window = [ce for ce in committed_prefix if base < ce.revision <= head_rev]

    # The latest committed tombstone in the window (a delete the incoming may not
    # have seen) and whether the entity is tombstoned at head.
    tombstone_rev = max((ce.revision for ce in window if ce.event.op == "tombstone"), default=None)
    ordered = sorted(committed_prefix, key=lambda ce: ce.revision)
    head_tombstoned = bool(ordered) and ordered[-1].event.op == "tombstone"

    if incoming.event.op == "tombstone":
        # delete-vs-delete is idempotent; a fresh delete applies.
        if head_tombstoned or tombstone_rev is not None:
            return MergeOutcome("delete_idempotent", (), head_rev)
        return MergeOutcome("apply", (), head_rev)

    if incoming.event.op == "append":
        # append_only collections never conflict (insert-once at the fold).
        return MergeOutcome("apply", (), head_rev)

    # patch: edit-vs-delete — a patch whose base predates a committed tombstone it
    # never saw stays deleted (the human revives it from the record), MOD-26 §B5.
    if tombstone_rev is not None and base < tombstone_rev:
        cid = conflict_record_id(
            incoming.event.entity_id, ENTITY_FIELD, [incoming.revision, tombstone_rev]
        )
        decision = FieldDecision(
            field=ENTITY_FIELD,
            verdict="conflict",
            contending_revision=tombstone_rev,
            head_value=None,  # the entity is deleted at head
            incoming_value=dict(incoming.event.payload),
            conflict_record_id=cid,
        )
        return MergeOutcome("edit_vs_delete", (decision,), head_rev)

    decisions: list[FieldDecision] = []
    for field, incoming_value in incoming.event.payload.items():
        if field == REVIVE_FIELD:
            continue
        contender = _field_head(window, field)
        if contender is None:
            decisions.append(FieldDecision(field, "apply", None, None, incoming_value, None))
            continue
        head_value = contender.event.payload.get(field)
        if _canon_eq(head_value, incoming_value):
            decisions.append(
                FieldDecision(
                    field, "idempotent", contender.revision, head_value, incoming_value, None
                )
            )
        else:
            cid = conflict_record_id(
                incoming.event.entity_id, field, [incoming.revision, contender.revision]
            )
            decisions.append(
                FieldDecision(
                    field, "conflict", contender.revision, head_value, incoming_value, cid
                )
            )

    field_decisions = tuple(decisions)
    if any(d.verdict == "conflict" for d in field_decisions):
        entity_verdict: EntityVerdict = "conflict"
    elif field_decisions and all(d.verdict == "idempotent" for d in field_decisions):
        entity_verdict = "idempotent"
    elif base < head_rev:
        entity_verdict = "auto_merge"
    else:
        entity_verdict = "apply"
    return MergeOutcome(entity_verdict, field_decisions, head_rev)
