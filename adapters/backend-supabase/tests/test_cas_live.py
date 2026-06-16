"""E05-T3 — the p_cas CAS path through the REAL adapter, on real Postgres + RLS.

`test_cas_rpc.py` pins the RPC's `p_cas` branch at the SQL level. This pins the
SAME behaviour through `SupabaseSyncBackend.commit_events` — the adapter's
HTTP/PostgREST-dialect path the runtime actually uses — against real Postgres via
`FakePostgREST`. So the #59 adapter wiring is verified end-to-end, not just the
plpgsql:
  • it sends `p_cas` to the RPC,
  • it parses the rich `conflicts[]` the RPC returns into `CommitResult.conflicts`,
  • it maps the RPC's `rebase_required` raise to `RebaseRequired`.

Unsigned / pre-cutover (`require_signature=False`), so no grant/signature setup —
the conflict + CAS decisions are policy-independent (the RPC's grant check is
skipped for unsigned events). Acts as the seeded `mbr_alice` in `ws_a`.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from kantaq_backend_supabase.sync import SupabaseSyncBackend
from kantaq_sync_engine.events import Event, RebaseRequired
from kantaq_test_harness.postgrest import FakePostgREST, encode_test_jwt
from kantaq_test_harness.rls import supabase_claims

ANON_KEY = "anon-key-for-tests"
_n = itertools.count(8000)


def _adapter(engine: Engine) -> SupabaseSyncBackend:
    fake = FakePostgREST(engine)
    token = encode_test_jwt(supabase_claims("alice@acme.dev"))
    return SupabaseSyncBackend(
        fake.base_url,
        ANON_KEY,
        workspace_id="ws_a",
        access_token=lambda: token,
        client=fake.client(),
    )


def _ev(entity: str, payload: dict[str, Any], *, base_rev: int | None = None) -> Event:
    return Event(
        event_id=f"evt_caslive{next(_n):010d}",
        collection="tickets",
        entity_id=entity,
        actor_id="mbr_alice",
        actor_seq=next(_n),
        op="patch",
        base_rev=base_rev,
        payload=payload,
    )


def _head_status(backend: SupabaseSyncBackend, entity: str) -> str | None:
    """The current committed status for the entity, read back through the adapter."""
    rows = backend.pull(collection="tickets")
    folded: dict[str, str] = {}
    for entry in sorted(rows, key=lambda e: e.revision):
        if entry.event.entity_id == entity and "status" in entry.event.payload:
            folded[entity] = entry.event.payload["status"]
    return folded.get(entity)


def test_adapter_surfaces_conflicts_from_the_real_rpc(sync_pg: Engine) -> None:
    """An ordinary (non-CAS) stale-and-contending write commits LWW and the
    adapter surfaces the RPC's per-field conflicts[] (what the client mints from)."""
    a = _adapter(sync_pg)
    ent = "tkt_caslive_a"
    r1 = a.commit_events([_ev(ent, {"status": "todo"})], require_signature=False)
    rev1 = r1[0].revision
    a.commit_events([_ev(ent, {"status": "doing"}, base_rev=rev1)], require_signature=False)
    # base=rev1 is now stale (doing committed after it) and contends on status.
    res = a.commit_events([_ev(ent, {"status": "done"}, base_rev=rev1)], require_signature=False)

    assert res[0].status == "committed"  # ordinary write rides LWW
    assert res[0].stale_base_rev == rev1
    fields = {c.field for c in res[0].conflicts}
    assert "status" in fields  # the adapter parsed the RPC's conflicts[]
    assert _head_status(a, ent) == "done"  # ordinary = ride-flagged (LWW value wins)


def test_adapter_cas_rejects_a_contending_write(sync_pg: Engine) -> None:
    """A CAS write (cas=True) that would contend → the RPC commits nothing and
    raises rebase_required; the adapter maps it to RebaseRequired. Nothing lands."""
    a = _adapter(sync_pg)
    ent = "tkt_caslive_b"
    r1 = a.commit_events([_ev(ent, {"status": "todo"})], require_signature=False)
    rev1 = r1[0].revision
    a.commit_events([_ev(ent, {"status": "doing"}, base_rev=rev1)], require_signature=False)

    with pytest.raises(RebaseRequired):
        a.commit_events(
            [_ev(ent, {"status": "done"}, base_rev=rev1)], require_signature=False, cas=True
        )

    # The stale CAS value never committed — the team's 'doing' still stands.
    assert _head_status(a, ent) == "doing"


def test_adapter_cas_commits_a_nonconflicting_write(sync_pg: Engine) -> None:
    """A CAS write on a field that did NOT move (different field) does not
    contend, so the RPC commits it — cas only bounces a genuine clash."""
    a = _adapter(sync_pg)
    ent = "tkt_caslive_c"
    r1 = a.commit_events([_ev(ent, {"status": "todo"})], require_signature=False)
    rev1 = r1[0].revision
    a.commit_events([_ev(ent, {"status": "doing"}, base_rev=rev1)], require_signature=False)
    # priority never moved → no contention even though base is stale.
    res = a.commit_events(
        [_ev(ent, {"priority": "high"}, base_rev=rev1)], require_signature=False, cas=True
    )

    assert res[0].status == "committed"
    assert res[0].conflicts == ()
