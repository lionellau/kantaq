"""E05-T2.6: the RPC's per-field conflicts[] == detect_merge, on real Postgres.

One decision, one truth (MOD-26 §B3): the SAME golden conflict_vectors.json that
pins detect_merge (Python, packages/sync_engine/tests/test_merge.py) is replayed
through the atomic commit RPC on EphemeralPostgres, and the rich conflicts[] the
RPC returns must equal what detect_merge computes on the SAME committed prefix.
This is the cross-language merge gate agreed in MOD-26 — the test_verb_map_parity
discipline applied to the merge rule, so the plpgsql and the client preview can
never silently disagree about what conflicts.

Each vector's revisions are vector-local; the RPC assigns its own. We commit the
prefix through the RPC (capturing the db revision each event gets), remap every
base_rev/contender through that, then run detect_merge over the ACTUAL committed
prefix so the two sides are compared on identical revisions.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from kantaq_protocol import Event, SchemaViolation, canonicalize
from kantaq_sync_engine import CommittedEvent
from kantaq_sync_engine.merge import detect_merge
from kantaq_test_harness.postgrest import (
    encode_test_jwt,  # noqa: F401 (parity w/ sibling test imports)
)
from kantaq_test_harness.rls import TamperedClient, supabase_claims
from kantaq_test_harness.vectors import load_conflict_vectors

_SIG = "ab" * 32  # presence placeholder; the RPC never verifies the bytes
_seq = itertools.count(5000)  # unique (actor, actor_seq) across every vector
_eid = itertools.count(1)


def _event_id() -> str:
    return f"e{next(_eid):025d}"


def _commit_one(
    engine: Engine, *, entity: str, op: str, payload: dict[str, Any], base_rev: int | None
) -> dict[str, Any]:
    """Commit one event as mbr_alice/grant_alice through the RPC; return its result."""
    event = {
        "event_id": _event_id(),
        "collection": "tickets",
        "entity_id": entity,
        "actor_id": "mbr_alice",
        "actor_seq": next(_seq),
        "op": op,
        "base_rev": base_rev,
        "policy_ref": "grant_alice",
        "payload": payload,
        "sig": _SIG,
        "workspace_id": "ws_a",
    }
    client = TamperedClient(engine, claims=supabase_claims("alice@acme.dev"))
    with client.session() as conn:
        result = conn.execute(
            text("select public.events(cast(:p as jsonb), true) as r"),
            {"p": json.dumps([event])},
        ).one()
        conn.commit()
    out = list(result.r)[0]
    return {**out, "_event": event}


def _norm(conflicts: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
    """A comparable set: field, contender revision, both candidate values."""
    return {
        (
            c["field"],
            c["contending_revision"],
            json.dumps(c["head_value"], sort_keys=True),
            json.dumps(c["incoming_value"], sort_keys=True),
        )
        for c in conflicts
    }


def _detect_conflicts(
    prefix: list[CommittedEvent], incoming: CommittedEvent
) -> set[tuple[Any, ...]]:
    outcome = detect_merge(prefix, incoming)
    return _norm(
        [
            {
                "field": d.field,
                "contending_revision": d.contending_revision,
                "head_value": d.head_value,
                "incoming_value": d.incoming_value,
            }
            for d in outcome.conflicts
        ]
    )


def _committed(
    entity: str, res: dict[str, Any], op: str, base: int | None, payload: dict[str, Any]
) -> CommittedEvent:
    return CommittedEvent(
        revision=int(res["revision"]),
        event=Event(
            event_id=res["_event"]["event_id"],
            collection="tickets",
            entity_id=entity,
            actor_id="mbr_alice",
            actor_seq=res["_event"]["actor_seq"],
            op=op,
            base_rev=base,
            policy_ref=None,
            payload=dict(payload),
            sig=None,
        ),
    )


_adv = itertools.count(1)


def _assert_parity(
    engine: Engine, prefix: list[dict[str, Any]], incoming: dict[str, Any], name: str
) -> None:
    """Commit a scenario (prefix then incoming) through the RPC and assert its
    conflicts[] equal detect_merge on the same committed prefix. ``base`` is a
    1-based index into ``prefix`` (or None for genesis)."""
    entity = f"adv{next(_adv):023d}"
    committed: list[CommittedEvent] = []
    for ev in prefix:
        idx = ev["base"]
        base = committed[idx - 1].revision if idx else None  # 0 or None → genesis
        res = _commit_one(
            engine, entity=entity, op=ev["op"], payload=dict(ev["payload"]), base_rev=base
        )
        committed.append(_committed(entity, res, ev["op"], base, ev["payload"]))
    idx = incoming["base"]
    base = committed[idx - 1].revision if idx else None
    res = _commit_one(
        engine, entity=entity, op=incoming["op"], payload=dict(incoming["payload"]), base_rev=base
    )
    inc = _committed(entity, res, incoming["op"], base, incoming["payload"])
    rpc = _norm(res["conflicts"])
    expected = _detect_conflicts(committed, inc)
    assert rpc == expected, f"{name}: RPC {rpc} != detect_merge {expected}"


# Adversarial scenarios from the divergence-hunt workflow (23 cases across 4
# lenses: multi-field/ordering, tombstone/revive, json-type-coercion,
# window-boundary). Each is replayed through the real RPC and asserted equal to
# detect_merge — so the plpgsql and the client preview cannot diverge on the
# edges the curated golden vectors miss (bool-vs-int, explicit-null field-head,
# tombstone short-circuit, far-back/multi-write field-head, large ints, nested
# values, base==head boundary).
_SCENARIOS_PATH = Path(__file__).parent / "fixtures" / "conflict_divergence_scenarios.json"
ADVERSARIAL: list[dict[str, Any]] = json.loads(_SCENARIOS_PATH.read_text())["scenarios"]


def _codec_forbidden(scenario: dict[str, Any]) -> list[Any]:
    """Payload values the signing codec (canonicalize) refuses. Such a value can
    never be a committed event — canonicalize is the bytes a signature commits
    to — so the merge never sees it, and a posited json-vs-jsonb divergence on it
    is unreachable. Returns the offending values (empty when the scenario is
    entirely codec-valid)."""
    forbidden: list[Any] = []
    for ev in [*scenario["prefix"], scenario["incoming"]]:
        for value in ev["payload"].values():
            try:
                canonicalize(value)
            except SchemaViolation:
                forbidden.append(value)
    return forbidden


def test_rpc_matches_detect_merge_on_adversarial_scenarios(sync_pg: Engine) -> None:
    """Every codec-valid hunt scenario: the RPC's conflicts[] == detect_merge."""
    ran = 0
    for scenario in ADVERSARIAL:
        if _codec_forbidden(scenario):
            continue  # codec-forbidden input — covered by the test below
        _assert_parity(sync_pg, scenario["prefix"], scenario["incoming"], scenario["name"])
        ran += 1
    assert ran >= 18  # the bulk of the hunt is codec-valid and runs here


def test_codec_forbidden_scenarios_are_unreachable_not_divergences() -> None:
    """The numeric divergences the hunt posited (float / >2^53 int) rest on values
    the signing codec refuses, so no signed event can carry them — canonicalize
    fails loud. This pins WHY those 'divergences' can never reach the merge."""
    forbidden = [s for s in ADVERSARIAL if _codec_forbidden(s)]
    assert forbidden  # at least the high-precision-int probe is here
    for scenario in forbidden:
        for value in _codec_forbidden(scenario):
            with pytest.raises(SchemaViolation):
                canonicalize(value)


def test_rpc_conflicts_match_detect_merge_on_every_golden_vector(sync_pg: Engine) -> None:
    vectors = load_conflict_vectors()
    assert vectors  # the file is non-empty (a missing fixture would silently pass)

    for index, vector in enumerate(vectors):
        entity = f"tktxv{index:021d}"  # a fresh entity per vector (26-char id)
        rev_map: dict[int, int] = {}
        committed: list[CommittedEvent] = []

        for ce in vector.committed_prefix:
            ev = ce.event
            base = rev_map[ev.base_rev] if ev.base_rev is not None else None
            res = _commit_one(
                sync_pg, entity=entity, op=ev.op, payload=dict(ev.payload), base_rev=base
            )
            db_rev = int(res["revision"])
            rev_map[ce.revision] = db_rev
            committed.append(
                CommittedEvent(
                    revision=db_rev,
                    event=Event(
                        event_id=res["_event"]["event_id"],
                        collection="tickets",
                        entity_id=entity,
                        actor_id="mbr_alice",
                        actor_seq=res["_event"]["actor_seq"],
                        op=ev.op,
                        base_rev=base,
                        policy_ref=None,
                        payload=dict(ev.payload),
                        sig=None,
                    ),
                )
            )

        inc = vector.incoming.event
        inc_base = rev_map[inc.base_rev] if inc.base_rev is not None else None
        res = _commit_one(
            sync_pg, entity=entity, op=inc.op, payload=dict(inc.payload), base_rev=inc_base
        )
        incoming = CommittedEvent(
            revision=int(res["revision"]),
            event=Event(
                event_id=res["_event"]["event_id"],
                collection="tickets",
                entity_id=entity,
                actor_id="mbr_alice",
                actor_seq=res["_event"]["actor_seq"],
                op=inc.op,
                base_rev=inc_base,
                policy_ref=None,
                payload=dict(inc.payload),
                sig=None,
            ),
        )

        rpc = _norm(res["conflicts"])
        expected = _detect_conflicts(committed, incoming)
        assert rpc == expected, (
            f"vector {vector.name!r}: RPC conflicts {rpc} != detect_merge {expected}"
        )
