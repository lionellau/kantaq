"""The v0.2 atomic commit RPC (E24-T6, FR-E24-3, D-09) on real Postgres.

The RPC (``supabase/rpc/events.sql``) is the v0.2 commit primitive: in ONE
transaction it validates grant + ordering against committed state, applies the
merge policy (LWW by commit order), assigns the revision, and reports staleness.
These tests prove, on the disposable Postgres with the checked-in artifacts
applied (``conftest.sync_pg``):

- it rejects unsigned / missing-grant / revoked / expired / wrong-subject /
  wrong-resource / wrong-verb / not-self events BEFORE applying any (atomic);
- it tolerates pre-cutover unsigned history (require_signature=False);
- it assigns the revision and reports stale_base_rev for an out-of-date base;
- an idempotent re-push is a duplicate, never a double-commit;
- a per-workspace advisory lock serialises concurrent writers so revision N
  commits before N+1 is assigned — closing the commit-visibility window.

The Ed25519 byte-check is deliberately NOT here: stock Postgres has no Ed25519
(D-09), so the signature bytes are verified client-side (see test_verify_*); the
RPC enforces everything checkable against committed state plus signature
presence. The ``sig`` values below are presence placeholders only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from kantaq_backend_supabase.sync import SupabaseSyncBackend, SyncBackendError
from kantaq_sync_engine import Event
from kantaq_test_harness.postgrest import FakePostgREST, encode_test_jwt
from kantaq_test_harness.rls import TamperedClient, supabase_claims

ALICE = "alice@acme.dev"
_SIG = "ab" * 32  # presence placeholder; the RPC never verifies the bytes


def _event(
    event_id: str,
    *,
    seq: int,
    actor: str = "mbr_alice",
    ws: str = "ws_a",
    collection: str = "tickets",
    entity: str = "tkt_rpc",
    op: str = "patch",
    base_rev: int | None = None,
    policy_ref: str | None = "grant_alice",
    sig: str | None = _SIG,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "collection": collection,
        "entity_id": entity,
        "actor_id": actor,
        "actor_seq": seq,
        "op": op,
        "base_rev": base_rev,
        "policy_ref": policy_ref,
        "payload": payload or {"title": "x"},
        "sig": sig,
        "workspace_id": ws,
    }


def _commit(
    engine: Engine,
    events: list[dict[str, Any]],
    *,
    email: str = ALICE,
    require_signature: bool = True,
) -> list[dict[str, Any]]:
    """Call the RPC as a member and COMMIT; return the parsed result array."""
    client = TamperedClient(engine, claims=supabase_claims(email))
    with client.session() as conn:
        result = conn.execute(
            text("select public.events(cast(:p as jsonb), :req) as r"),
            {"p": json.dumps(events), "req": require_signature},
        ).one()
        conn.commit()
        return list(result.r)


def _reject(
    engine: Engine,
    events: list[dict[str, Any]],
    *,
    email: str = ALICE,
    require_signature: bool = True,
) -> str:
    """Call the RPC expecting it to RAISE; return the error text (atomic — the
    session is rolled back, so nothing committed)."""
    client = TamperedClient(engine, claims=supabase_claims(email))
    with pytest.raises(Exception) as exc, client.session() as conn:  # noqa: PT011 - assert on message
        conn.execute(
            text("select public.events(cast(:p as jsonb), :req)"),
            {"p": json.dumps(events), "req": require_signature},
        )
        conn.commit()
    return str(exc.value)


def _add_device(engine: Engine, did: str, *, revoked: bool = False) -> None:
    revoked_at = "now()" if revoked else "null"
    TamperedClient(engine, claims={}, role="service_role").attempt(
        "insert into devices (id, created_at, updated_at, actor_seq, visibility, hosting_mode,"
        f" retention_policy, public_key, member_id, label, revoked_at) values ('{did}', now(),"
        f" now(), 0, 'team', 'plain', 'standard', '{'c' * 64}', 'mbr_alice', 'd', {revoked_at})"
    )


def _add_grant(
    engine: Engine,
    gid: str,
    *,
    subject: str = "mbr_alice",
    issuer: str = "dev_alice",
    resource: str = "ws_a",
    verbs: tuple[str, ...] = ("tickets.write",),
    issued_at: int = 0,
    expires_at: int = 2000000000,
    revoked: bool = False,
) -> None:
    revoked_at = "now()" if revoked else "null"
    verbs_json = json.dumps(list(verbs))
    TamperedClient(engine, claims={}, role="service_role").attempt(
        "insert into capability_grants (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, subject, issuer, resource, verbs, issued_at,"
        f" expires_at, sig, revoked_at) values ('{gid}', now(), now(), 0, 'team', 'plain',"
        f" 'standard', '{subject}', '{issuer}', '{resource}', '{verbs_json}'::json,"
        f" {issued_at}, {expires_at}, '{_SIG}', {revoked_at})"
    )


def _events_for(engine: Engine, event_id: str) -> list[Any]:
    return TamperedClient(engine, claims={}, role="service_role").fetch_all(
        f"select revision, payload from sync_events where event_id = '{event_id}'"
    )


# --- happy path ---------------------------------------------------------------


def test_a_signed_grant_authorised_event_commits_and_assigns_a_revision(sync_pg: Engine) -> None:
    result = _commit(sync_pg, [_event("evt_rpc_0000000000000001", seq=10)])
    assert len(result) == 1
    [out] = result
    assert out["status"] == "committed"
    assert out["revision"] > 0
    assert out["stale_base_rev"] is None
    rows = _events_for(sync_pg, "evt_rpc_0000000000000001")
    assert len(rows) == 1 and rows[0].revision == out["revision"]


def test_a_batch_commits_every_event_in_submission_order(sync_pg: Engine) -> None:
    batch = [_event(f"evt_rpc_batch_{i:010d}", seq=20 + i, entity=f"tkt_{i}") for i in range(3)]
    result = _commit(sync_pg, batch)
    revisions = [r["revision"] for r in result]
    assert revisions == sorted(revisions)
    assert all(r["status"] == "committed" for r in result)


# --- reject before applying (atomic) ------------------------------------------


def test_an_unsigned_event_is_rejected_post_cutover(sync_pg: Engine) -> None:
    error = _reject(sync_pg, [_event("evt_rpc_unsigned00000001", seq=30, sig=None)])
    assert "unsigned" in error
    assert _events_for(sync_pg, "evt_rpc_unsigned00000001") == []


def test_a_missing_grant_is_policy_denied(sync_pg: Engine) -> None:
    error = _reject(sync_pg, [_event("evt_rpc_nogrant00000001", seq=31, policy_ref="grant_ghost")])
    assert "policy_denied" in error and "not held" in error


def test_a_revoked_grant_is_policy_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_revoked", revoked=True)
    error = _reject(
        sync_pg, [_event("evt_rpc_revoked00000001", seq=32, policy_ref="grant_revoked")]
    )
    assert "policy_denied" in error and "revoked" in error


def test_an_expired_grant_is_policy_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_expired", issued_at=0, expires_at=1)  # expired in 1970
    error = _reject(
        sync_pg, [_event("evt_rpc_expired00000001", seq=33, policy_ref="grant_expired")]
    )
    assert "policy_denied" in error and "expired" in error


def test_a_grant_whose_subject_is_not_the_actor_is_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_other", subject="mbr_bob")
    error = _reject(sync_pg, [_event("evt_rpc_subj00000000001", seq=34, policy_ref="grant_other")])
    assert "policy_denied" in error and "actor" in error


def test_a_grant_whose_resource_is_another_workspace_is_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_ws_b", resource="ws_b")
    error = _reject(sync_pg, [_event("evt_rpc_res000000000001", seq=35, policy_ref="grant_ws_b")])
    assert "policy_denied" in error and "workspace" in error


def test_a_grant_without_the_collection_verb_is_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_noverb", verbs=("proposals.write",))
    error = _reject(
        sync_pg, [_event("evt_rpc_verb00000000001", seq=36, policy_ref="grant_noverb")]
    )
    assert "policy_denied" in error and "tickets" in error


def test_a_grant_from_a_revoked_issuer_device_is_denied(sync_pg: Engine) -> None:
    """Parity with verify_grant: a revoked issuer device is no longer a live
    verification root, so a grant it issued authorises nothing."""
    _add_device(sync_pg, "dev_dead", revoked=True)
    _add_grant(sync_pg, "grant_deadissuer", issuer="dev_dead")
    error = _reject(
        sync_pg, [_event("evt_rpc_deadiss00001", seq=38, policy_ref="grant_deadissuer")]
    )
    assert "policy_denied" in error and "issuer" in error


def test_a_grant_whose_issuer_device_is_absent_is_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_noissuer", issuer="dev_ghost000000000000000001")
    error = _reject(
        sync_pg, [_event("evt_rpc_noiss0000001", seq=39, policy_ref="grant_noissuer")]
    )
    assert "policy_denied" in error and "issuer" in error


def test_a_grant_with_an_inverted_validity_window_is_denied(sync_pg: Engine) -> None:
    _add_grant(sync_pg, "grant_inverted", issued_at=1000, expires_at=500)
    error = _reject(
        sync_pg, [_event("evt_rpc_inverted0001", seq=44, policy_ref="grant_inverted")]
    )
    assert "policy_denied" in error and "validity" in error


def test_an_actor_who_is_not_the_caller_is_denied(sync_pg: Engine) -> None:
    # alice is the caller, but the event claims to be from bob.
    error = _reject(
        sync_pg,
        [_event("evt_rpc_notself00000001", seq=37, actor="mbr_bob", policy_ref="grant_alice")],
    )
    assert "policy_denied" in error and "caller" in error


def test_a_batch_with_one_bad_event_commits_nothing(sync_pg: Engine) -> None:
    """Atomic reject: a good event beside a bad one must not slip through."""
    batch = [
        _event("evt_rpc_good00000000001", seq=40),
        _event("evt_rpc_bad000000000001", seq=41, sig=None),  # unsigned → whole batch fails
    ]
    error = _reject(sync_pg, batch)
    assert "unsigned" in error
    assert _events_for(sync_pg, "evt_rpc_good00000000001") == []


def test_a_pass_two_failure_rolls_back_already_inserted_events(sync_pg: Engine) -> None:
    """Whole-transaction atomicity past validation: if a later event in the
    batch fails to INSERT (here a UNIQUE(event_id) collision), the earlier
    event that already inserted is rolled back too — nothing commits."""
    batch = [
        _event("evt_rpc_first0000001", seq=45, entity="tkt_p2a"),
        # same event_id as the first but a fresh actor_seq → UNIQUE(event_id)
        # violation in pass 2, AFTER the first row was inserted.
        _event("evt_rpc_first0000001", seq=46, entity="tkt_p2b"),
    ]
    _reject(sync_pg, batch)  # raises (23505 unique_violation)
    assert _events_for(sync_pg, "evt_rpc_first0000001") == []


def test_a_reused_event_id_with_a_new_floor_is_a_hard_error(sync_pg: Engine) -> None:
    """An event_id is globally unique; re-using it with a different
    (actor_id, actor_seq) is a client bug, not an idempotent re-push, and is
    rejected (nothing commits) rather than silently deduped."""
    _commit(sync_pg, [_event("evt_rpc_reuse0000001", seq=47)])
    _reject(sync_pg, [_event("evt_rpc_reuse0000001", seq=48, entity="tkt_other")])
    # exactly one row (the original), unchanged
    assert len(_events_for(sync_pg, "evt_rpc_reuse0000001")) == 1


# --- pre-cutover tolerance ----------------------------------------------------


def test_pre_cutover_unsigned_history_is_tolerated(sync_pg: Engine) -> None:
    result = _commit(
        sync_pg,
        [_event("evt_rpc_precut00000001", seq=42, sig=None, policy_ref=None)],
        require_signature=False,
    )
    assert result[0]["status"] == "committed"
    assert len(_events_for(sync_pg, "evt_rpc_precut00000001")) == 1


# --- merge policy: staleness + idempotency ------------------------------------


def test_a_stale_base_rev_is_reported_but_still_commits_lww(sync_pg: Engine) -> None:
    """A second writer whose base_rev predates the committed head commits (LWW by
    order) but is told it was stale, so it can mint a conflict_record (E05-T2)."""
    first = _commit(sync_pg, [_event("evt_rpc_head000000001", seq=50, entity="tkt_conf")])
    head = first[0]["revision"]
    second = _commit(
        sync_pg,
        [_event("evt_rpc_stale0000001", seq=51, entity="tkt_conf", base_rev=head - 1)],
    )
    assert second[0]["status"] == "committed"
    assert second[0]["stale_base_rev"] == head - 1
    assert second[0]["head_rev"] == head


def test_a_fresh_base_rev_is_not_flagged_stale(sync_pg: Engine) -> None:
    first = _commit(sync_pg, [_event("evt_rpc_fresh0000001", seq=52, entity="tkt_fresh")])
    head = first[0]["revision"]
    second = _commit(
        sync_pg,
        [_event("evt_rpc_fresh0000002", seq=53, entity="tkt_fresh", base_rev=head)],
    )
    assert second[0]["stale_base_rev"] is None


def test_an_idempotent_repush_is_a_duplicate_not_a_double_commit(sync_pg: Engine) -> None:
    first = _commit(sync_pg, [_event("evt_rpc_idem00000001", seq=54)])
    again = _commit(sync_pg, [_event("evt_rpc_idem00000001", seq=54)])
    assert first[0]["status"] == "committed"
    assert again[0]["status"] == "duplicate"
    assert again[0]["revision"] == first[0]["revision"]
    # a duplicate did not commit now, so its merge metadata is not meaningful
    assert again[0]["stale_base_rev"] is None
    assert again[0]["head_rev"] is None
    assert again[0]["base_rev"] is None
    assert len(_events_for(sync_pg, "evt_rpc_idem00000001")) == 1


def test_trust_root_events_are_accepted_without_a_verb_gate(sync_pg: Engine) -> None:
    """DEBT-15(a/b), pinned as intentional: devices/capability_grants have no
    per-verb requirement yet, so the caller's valid grant authorises a trust-root
    write. The peer-trust verb model (which grant may mint which root) is the
    deferred work; this test pins today's behavior so the gap is visible, not
    silent. The is_self_in_workspace + valid-grant walls still apply."""
    result = _commit(
        sync_pg,
        [_event("evt_rpc_trustroot001", seq=55, collection="devices", entity="dev_new")],
    )
    assert result[0]["status"] == "committed"


# --- concurrent writers never expose N+1 before N -----------------------------


def test_concurrent_writers_serialise_on_the_per_workspace_lock(sync_pg: Engine) -> None:
    """The advisory xact lock serialises commits per workspace: while writer A
    holds it (mid-transaction), writer B for the SAME workspace blocks — proven
    by a lock_timeout firing. So revision N commits before N+1 is even assigned,
    closing the v0.1 commit-visibility window (exit criterion 2)."""
    alice = TamperedClient(sync_pg, claims=supabase_claims(ALICE))
    with alice.session() as conn_a:
        # A acquires the per-workspace lock and inserts, but does NOT commit yet.
        conn_a.execute(
            text("select public.events(cast(:p as jsonb), true)"),
            {"p": json.dumps([_event("evt_rpc_lock00000001", seq=60)])},
        )
        # B, for the same workspace, must block on A's lock → lock_timeout fires.
        with alice.session() as conn_b:
            conn_b.execute(text("set local lock_timeout = '400ms'"))
            with pytest.raises(Exception) as exc:
                conn_b.execute(
                    text("select public.events(cast(:p as jsonb), true)"),
                    {"p": json.dumps([_event("evt_rpc_lock00000002", seq=61)])},
                )
            assert "lock" in str(exc.value).lower()


# --- through the real adapter (commit_events over FakePostgREST) --------------


def _adapter(engine: Engine, email: str = ALICE, workspace_id: str = "ws_a") -> SupabaseSyncBackend:
    fake = FakePostgREST(engine)
    token = encode_test_jwt(supabase_claims(email))
    return SupabaseSyncBackend(
        fake.base_url,
        "anon-key-for-tests",
        workspace_id=workspace_id,
        access_token=lambda: token,
        client=fake.client(),
    )


def _adapter_event(event_id: str, seq: int, **over: Any) -> Event:
    return Event(
        event_id=event_id,
        collection=over.get("collection", "tickets"),
        entity_id=over.get("entity", "tkt_ad"),
        actor_id="mbr_alice",
        actor_seq=seq,
        op="patch",
        base_rev=over.get("base_rev"),
        policy_ref=over.get("policy_ref", "grant_alice"),
        payload=over.get("payload", {"title": "via adapter"}),
        sig=over.get("sig", _SIG),
    )


def test_adapter_commit_events_returns_structured_results(sync_pg: Engine) -> None:
    adapter = _adapter(sync_pg)
    results = adapter.commit_events([_adapter_event("evt_ad_00000000000001", 70)])
    assert len(results) == 1
    [out] = results
    assert out.status == "committed"
    assert out.revision > 0
    assert out.is_stale is False
    # the event is in the log at the returned revision
    assert _events_for(sync_pg, "evt_ad_00000000000001")[0].revision == out.revision


def test_adapter_commit_events_raises_on_a_rejected_event(sync_pg: Engine) -> None:
    adapter = _adapter(sync_pg)
    with pytest.raises(SyncBackendError):
        adapter.commit_events([_adapter_event("evt_ad_00000000000002", 71, sig=None)])
    # atomic: nothing committed
    assert _events_for(sync_pg, "evt_ad_00000000000002") == []


def test_a_different_workspace_does_not_block(sync_pg: Engine) -> None:
    """The lock is per-workspace: a concurrent writer in another workspace is
    never blocked by a holder in ours."""
    alice = TamperedClient(sync_pg, claims=supabase_claims(ALICE))
    cher = TamperedClient(sync_pg, claims=supabase_claims("cher@other.dev"))
    _add_grant(sync_pg, "grant_cher", subject="mbr_cher", resource="ws_b")
    with alice.session() as conn_a:
        conn_a.execute(
            text("select public.events(cast(:p as jsonb), true)"),
            {"p": json.dumps([_event("evt_rpc_wsA00000001", seq=62)])},
        )
        with cher.session() as conn_b:
            conn_b.execute(text("set local lock_timeout = '2s'"))
            # ws_b, different advisory lock — must NOT block.
            conn_b.execute(
                text("select public.events(cast(:p as jsonb), true)"),
                {
                    "p": json.dumps(
                        [
                            _event(
                                "evt_rpc_wsB00000001",
                                seq=63,
                                actor="mbr_cher",
                                ws="ws_b",
                                policy_ref="grant_cher",
                            )
                        ]
                    )
                },
            )
            conn_b.commit()
