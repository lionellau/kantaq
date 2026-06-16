"""E05-T3: the RPC's p_cas (compare-and-swap) branch, on real Postgres.

A conflict resolution and an approved agent proposal commit through the atomic
RPC with ``p_cas := true``: if the write WOULD contend with the committed field
head, the RPC commits NOTHING and raises ``rebase_required`` (errcode 40001).
This is what keeps a stale resolution/proposal value from silently landing
(the resolver-vs-writer + propose-first holes, MOD-26 §B3/B4). A CAS write that
does not contend (saw the latest, or touched a different field) commits normally.

Verified on EphemeralPostgres because the reject must hold against the real
plpgsql under the per-workspace advisory lock — a commit-and-flag would land the
loser; only a true raise-and-rollback is the CAS.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from kantaq_test_harness.rls import TamperedClient, supabase_claims

_SIG = "ab" * 32
_seq = itertools.count(7000)
_eid = itertools.count(900000)


def _call(engine: Engine, event: dict[str, Any], *, cas: bool) -> list[dict[str, Any]]:
    client = TamperedClient(engine, claims=supabase_claims("alice@acme.dev"))
    with client.session() as conn:
        row = conn.execute(
            text("select public.events(cast(:p as jsonb), true, cast(:cas as boolean)) as r"),
            {"p": json.dumps([event]), "cas": cas},
        ).one()
        conn.commit()
        return list(row.r)


def _event(
    entity: str, payload: dict[str, Any], *, base_rev: int | None, op: str = "patch"
) -> dict:
    return {
        "event_id": f"e{next(_eid):025d}",
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


def _head(engine: Engine, entity: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "select coalesce(max(revision),0) from public.sync_events "
                "where workspace_id='ws_a' and collection='tickets' and entity_id=:e"
            ),
            {"e": entity},
        ).scalar_one()


def test_cas_rejects_a_contending_write_and_commits_nothing(sync_pg: Engine) -> None:
    ent = "tkt_cas1"
    r1 = _call(sync_pg, _event(ent, {"status": "todo"}, base_rev=None), cas=False)
    rev1 = r1[0]["revision"]
    # The team moves status forward (base = rev1) → head advances.
    _call(sync_pg, _event(ent, {"status": "doing"}, base_rev=rev1), cas=False)
    head_after_team = _head(sync_pg, ent)

    # A CAS write on the SAME field with the stale base (rev1) must be refused.
    with pytest.raises(DBAPIError) as exc:
        _call(sync_pg, _event(ent, {"status": "done"}, base_rev=rev1), cas=True)
    assert "rebase_required" in str(exc.value).lower()

    # Nothing committed: the head is unchanged and still reads the team's value.
    assert _head(sync_pg, ent) == head_after_team
    with sync_pg.connect() as conn:
        latest = conn.execute(
            text(
                "select payload::jsonb ->> 'status' from public.sync_events "
                "where workspace_id='ws_a' and entity_id=:e order by revision desc limit 1"
            ),
            {"e": ent},
        ).scalar_one()
    assert latest == "doing"  # the stale 'done' never landed


def test_cas_commits_a_nonconflicting_write(sync_pg: Engine) -> None:
    ent = "tkt_cas2"
    r1 = _call(sync_pg, _event(ent, {"status": "todo"}, base_rev=None), cas=False)
    rev1 = r1[0]["revision"]
    _call(sync_pg, _event(ent, {"status": "doing"}, base_rev=rev1), cas=False)

    # A CAS write on a DIFFERENT field (priority), stale base — does not contend,
    # so it commits (auto-merge), even under cas.
    out = _call(sync_pg, _event(ent, {"priority": "high"}, base_rev=rev1), cas=True)
    assert out[0]["status"] == "committed"
    assert out[0]["conflicts"] == []


def test_cas_commits_a_fresh_write(sync_pg: Engine) -> None:
    ent = "tkt_cas3"
    r1 = _call(sync_pg, _event(ent, {"status": "todo"}, base_rev=None), cas=False)
    head = r1[0]["revision"]
    # base == head: the write saw the latest → not stale, commits under cas.
    out = _call(sync_pg, _event(ent, {"status": "doing"}, base_rev=head), cas=True)
    assert out[0]["status"] == "committed"
