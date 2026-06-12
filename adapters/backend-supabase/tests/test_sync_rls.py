"""RLS on the shared sync log: the tampered client must fail (E24-T4, D-03).

Same discipline as ``test_rls.py``: direct SQL under the ``authenticated``
role with forged claims, every deny test next to a positive control, both
RLS deny shapes accepted (a raised WITH CHECK / grant error, or a USING
filter matching nothing). Runs on the disposable Postgres with the four
checked-in artifacts applied (``conftest.sync_pg``).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from kantaq_backend_supabase.schema import SYNC_POLICIES_FILE, read_repo_sql
from kantaq_test_harness.rls import TamperedClient, apply_sql, supabase_claims


def member(engine: Engine, email: str) -> TamperedClient:
    return TamperedClient(engine, claims=supabase_claims(email))


def service(engine: Engine) -> TamperedClient:
    return TamperedClient(engine, claims={}, role="service_role")


def _event_values(
    event_id: str, actor_id: str, actor_seq: int, workspace_id: str, collection: str = "tickets"
) -> str:
    return (
        f"('{event_id}', '{collection}', 'tkt_x', '{actor_id}', {actor_seq}, 'patch', "
        f"'{{}}'::json, '{workspace_id}')"
    )


INSERT = (
    "insert into sync_events (event_id, collection, entity_id, actor_id, actor_seq, op, "
    "payload, workspace_id) values "
)


# --- positive controls --------------------------------------------------------


def test_a_member_reads_and_writes_their_workspaces_log(sync_pg: Engine) -> None:
    bob = member(sync_pg, "bob@acme.dev")
    assert [r.entity_id for r in bob.fetch_all("select entity_id from sync_events")] == ["tkt_a"]
    own = bob.attempt(INSERT + _event_values("evt_bob_0000000000000001", "mbr_bob", 1, "ws_a"))
    assert own.ok and own.rowcount == 1


def test_revisions_are_strictly_monotonic_in_commit_order(sync_pg: Engine) -> None:
    bob = member(sync_pg, "bob@acme.dev")
    for seq in (1, 2, 3):
        assert bob.attempt(
            INSERT + _event_values(f"evt_bob_00000000000000{seq:02d}", "mbr_bob", seq, "ws_a")
        ).ok
    rows = service(sync_pg).fetch_all(
        "select revision from sync_events where actor_id = 'mbr_bob' order by actor_seq"
    )
    revisions = [r.revision for r in rows]
    assert revisions == sorted(revisions) and len(set(revisions)) == 3


# --- the tampered client ------------------------------------------------------


def test_tampered_client_cannot_read_another_workspaces_events(sync_pg: Engine) -> None:
    bob = member(sync_pg, "bob@acme.dev")
    leaked = bob.fetch_all("select event_id from sync_events where workspace_id = 'ws_b'")
    assert leaked == [], "workspace B's events leaked to a member of A"


def test_tampered_client_cannot_push_into_another_workspace(sync_pg: Engine) -> None:
    bob = member(sync_pg, "bob@acme.dev")
    cross = bob.attempt(INSERT + _event_values("evt_evil_000000000000001", "mbr_bob", 9, "ws_b"))
    assert cross.denied, "a member of A pushed an event into workspace B's log"


def test_event_authorship_cannot_be_forged(sync_pg: Engine) -> None:
    """The F3 rule on the log: you push only your own events."""
    bob = member(sync_pg, "bob@acme.dev")
    forged = bob.attempt(INSERT + _event_values("evt_evil_000000000000002", "mbr_alice", 9, "ws_a"))
    assert forged.denied, "bob pushed an event in alice's name"
    cross_actor = bob.attempt(
        INSERT + _event_values("evt_evil_000000000000003", "mbr_cher", 9, "ws_a")
    )
    assert cross_actor.denied, "bob pushed an event as another workspace's member"


def test_the_log_is_append_only_even_for_the_owner(sync_pg: Engine) -> None:
    alice = member(sync_pg, "alice@acme.dev")  # Owner of workspace A
    rewrite = alice.attempt(
        "update sync_events set payload = '{}'::json where event_id = 'evt_seed_a0000000000000000'"
    )
    assert not rewrite.ok and "permission denied" in rewrite.error
    erase = alice.attempt("delete from sync_events where event_id = 'evt_seed_a0000000000000000'")
    assert not erase.ok and "permission denied" in erase.error


def test_anon_gets_nothing(sync_pg: Engine) -> None:
    anon = TamperedClient(sync_pg, claims={}, role="anon")
    attempt = anon.attempt("select event_id from sync_events")
    assert not attempt.ok and "permission denied" in attempt.error


def test_a_revoked_member_loses_the_log(sync_pg: Engine) -> None:
    revoked = member(sync_pg, "rev@acme.dev")
    assert revoked.fetch_all("select event_id from sync_events") == []
    push = revoked.attempt(INSERT + _event_values("evt_rev_0000000000000001", "mbr_rev", 1, "ws_a"))
    assert push.denied, "a revoked member pushed to the log"


# --- database-level integrity (holds even past RLS) ---------------------------


def test_unsyncable_collections_are_refused_at_the_database(sync_pg: Engine) -> None:
    """tokens/audit_events never sync (MOD-04), and devices/capability_grants
    stay off the surface until E24-T5's verified ingestion (E27 review) —
    pinned by a CHECK constraint that binds even the service role."""
    excluded = ("tokens", "audit_events", "devices", "capability_grants")
    for seq, collection in enumerate(excluded, start=50):
        smuggled = service(sync_pg).attempt(
            INSERT
            + _event_values(
                f"evt_smug_{collection[:8].ljust(15, '0')}", "mbr_alice", seq, "ws_a", collection
            )
        )
        assert not smuggled.ok, f"{collection} events reached the shared log"
        assert "ck_sync_events_collection" in smuggled.error


def test_memory_collections_are_accepted_at_the_database(sync_pg: Engine) -> None:
    """The E13 regression, proven on real Postgres: team memory events pass
    the constraint (the local emit seam already keeps local rows out)."""
    for seq, collection in enumerate(("memory_entries", "memory_links"), start=60):
        accepted = service(sync_pg).attempt(
            INSERT
            + _event_values(
                f"evt_mem_{collection[7:15].ljust(16, '0')}", "mbr_alice", seq, "ws_a", collection
            )
        )
        assert accepted.ok, f"{collection} event was refused: {accepted.error}"


def test_duplicate_actor_seq_is_impossible(sync_pg: Engine) -> None:
    """UNIQUE (actor_id, actor_seq) is the dedup floor (NFR-E04-2)."""
    first = service(sync_pg).attempt(
        INSERT + _event_values("evt_dup_0000000000000001", "mbr_alice", 77, "ws_a")
    )
    assert first.ok
    dup = service(sync_pg).attempt(
        INSERT + _event_values("evt_dup_0000000000000002", "mbr_alice", 77, "ws_a")
    )
    assert not dup.ok and "sync_events_actor_id_actor_seq_key" in dup.error


def test_on_conflict_do_nothing_skips_duplicates_quietly(sync_pg: Engine) -> None:
    """The exact SQL shape PostgREST's resolution=ignore-duplicates produces."""
    alice = member(sync_pg, "alice@acme.dev")
    retry = alice.attempt(
        INSERT
        + _event_values("evt_seed_a0000000000000000", "mbr_alice", 1, "ws_a")
        + " on conflict (actor_id, actor_seq) do nothing"
    )
    assert retry.ok and retry.rowcount == 0  # skipped, not duplicated, no error


def test_on_conflict_do_update_cannot_rewrite_history(sync_pg: Engine) -> None:
    """Append-only at the grant layer: an upsert that tries to MUTATE on
    conflict is refused even though the bare INSERT is allowed — the member
    holds INSERT but never UPDATE, so DO UPDATE can never rewrite a committed
    event."""
    alice = member(sync_pg, "alice@acme.dev")
    upsert = alice.attempt(
        INSERT
        + _event_values("evt_dup_0000000000000099", "mbr_alice", 1, "ws_a")
        + " on conflict (actor_id, actor_seq) do update set payload = '{\"x\": 1}'::json"
    )
    assert not upsert.ok and "permission denied" in upsert.error
    untouched = service(sync_pg).fetch_all(
        "select payload from sync_events where event_id = 'evt_seed_a0000000000000000'"
    )
    assert untouched[0].payload == {"title": "A ticket"}


# --- grants + hygiene ---------------------------------------------------------


def test_sync_grants_match_the_documented_ceiling(sync_pg: Engine) -> None:
    with sync_pg.connect() as conn:
        rows = conn.execute(
            text(
                "select grantee, privilege_type from information_schema.role_table_grants"
                " where table_schema = 'public' and table_name = 'sync_events'"
                "   and grantee in ('anon', 'authenticated')"
            )
        ).all()
    assert [r for r in rows if r.grantee == "anon"] == []
    granted = {r.privilege_type for r in rows if r.grantee == "authenticated"}
    assert granted == {"SELECT", "INSERT"}, f"sync_events must be append-only, got {granted}"


def test_rls_is_enabled_on_the_log(sync_pg: Engine) -> None:
    with sync_pg.connect() as conn:
        row = conn.execute(
            text("select relrowsecurity from pg_class where relname = 'sync_events'")
        ).one()
    assert row.relrowsecurity is True


def test_sync_policies_file_reapplies_cleanly(sync_pg: Engine) -> None:
    apply_sql(sync_pg, read_repo_sql(SYNC_POLICIES_FILE))
    bob = member(sync_pg, "bob@acme.dev")
    assert [r.entity_id for r in bob.fetch_all("select entity_id from sync_events")] == ["tkt_a"]
