"""RLS against a real Postgres: the tampered client must fail (E24-T3, D-03).

The Backend profile's core requirement (harness standard §4): a client that
bypasses every app-layer check — talking straight to Postgres with forged
claims under the ``authenticated`` role, exactly what an attacker with a leaked
anon key + valid login could do — must not read or write outside its workspace.

Opt-in via ``KANTAQ_TEST_POSTGRES_URL`` (the CI Postgres service provides it;
locally the suite skips). Each test gets a fresh disposable database with the
*checked-in* SQL artifacts applied — the same files the maintainer applies to
Supabase — so what is tested is what ships.

Fail-closed discipline: every deny test sits next to a positive control, so a
policy bug that denies everything cannot masquerade as a pass.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from kantaq_backend_supabase.schema import (
    COLLECTIONS_MIGRATION,
    POLICIES_FILE,
    read_repo_sql,
)
from kantaq_db.models import COLLECTION_MODELS
from kantaq_db.parity import reflect_structure
from kantaq_test_harness.db import EphemeralPostgres
from kantaq_test_harness.rls import (
    TamperedClient,
    apply_sql,
    install_supabase_auth_stub,
    supabase_claims,
)

pytestmark = pytest.mark.skipif(
    not EphemeralPostgres.available(),
    reason="no KANTAQ_TEST_POSTGRES_URL (the CI Postgres service provides one)",
)

COLLECTION_TABLES = [model.__tablename__ for model in COLLECTION_MODELS]

# Two workspaces; the tampered client belongs to A and aims at B.
ENVELOPE = "now(), now(), 0, 'team', 'plain', 'standard'"
SEED = f"""
insert into workspaces (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, name) values
  ('ws_a', {ENVELOPE}, 'Acme'),
  ('ws_b', {ENVELOPE}, 'Other');

insert into members (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, workspace_id, email, role, status) values
  ('mbr_alice', {ENVELOPE}, 'ws_a', 'alice@acme.dev', 'Owner', 'active'),
  ('mbr_max',   {ENVELOPE}, 'ws_a', 'max@acme.dev',   'Maintainer', 'active'),
  ('mbr_bob',   {ENVELOPE}, 'ws_a', 'bob@acme.dev',   'Member', 'active'),
  ('mbr_rev',   {ENVELOPE}, 'ws_a', 'rev@acme.dev',   'Member', 'revoked'),
  ('mbr_cher',  {ENVELOPE}, 'ws_b', 'cher@other.dev', 'Owner', 'active');

insert into projects (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, workspace_id, name, goal, scope, status) values
  ('prj_a',  {ENVELOPE}, 'ws_a', 'AcmeApp', '', '', 'active'),
  ('prj_a2', {ENVELOPE}, 'ws_a', 'AcmeApp 2', '', '', 'active'),
  ('prj_b',  {ENVELOPE}, 'ws_b', 'OtherApp', '', '', 'active');

insert into tickets (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, project_id, title, description, status, priority, labels,
  acceptance_criteria, lifecycle_stage) values
  ('tkt_a', {ENVELOPE}, 'prj_a', 'A ticket', '', 'todo', 'medium', '[]'::json, '', 'intake'),
  ('tkt_b', {ENVELOPE}, 'prj_b', 'B ticket', '', 'todo', 'medium', '[]'::json, '', 'intake');

insert into comments (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, ticket_id, author_actor_id, body) values
  ('cmt_a', {ENVELOPE}, 'tkt_a', 'mbr_alice', 'on A'),
  ('cmt_b', {ENVELOPE}, 'tkt_b', 'mbr_cher', 'on B');

insert into tokens (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, member_id, hashed, scopes) values
  ('tok_alice', {ENVELOPE}, 'mbr_alice', '$argon2id$hash-a', '[]'::json),
  ('tok_bob',   {ENVELOPE}, 'mbr_bob',   '$argon2id$hash-b', '[]'::json),
  ('tok_cher',  {ENVELOPE}, 'mbr_cher',  '$argon2id$hash-c', '[]'::json);

insert into audit_events (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, actor_id, action, source) values
  ('aud_a', {ENVELOPE}, 'mbr_alice', 'ticket.update', 'app'),
  ('aud_b', {ENVELOPE}, 'mbr_cher', 'ticket.update', 'app');

insert into agent_proposals (id, created_at, updated_at, actor_seq, visibility, hosting_mode,
  retention_policy, ticket_id, proposer_id, diff, status) values
  ('prop_a', {ENVELOPE}, 'tkt_a', 'mbr_alice', '{{}}'::json, 'pending'),
  ('prop_b', {ENVELOPE}, 'tkt_b', 'mbr_cher', '{{}}'::json, 'pending');
"""

# Per-table probes at workspace B's rows: the tampered read must return none.
CROSS_WORKSPACE_READS = [
    ("workspaces", "select id from workspaces where id = 'ws_b'"),
    ("projects", "select id from projects where id = 'prj_b'"),
    ("tickets", "select id from tickets where id = 'tkt_b'"),
    ("comments", "select id from comments where id = 'cmt_b'"),
    ("members", "select id from members where workspace_id = 'ws_b'"),
    ("tokens", "select id from tokens where id = 'tok_cher'"),
    ("audit_events", "select id from audit_events where id = 'aud_b'"),
    ("agent_proposals", "select id from agent_proposals where id = 'prop_b'"),
]


@pytest.fixture
def backend() -> Iterator[Engine]:
    """A disposable Postgres with the checked-in artifacts applied and seeded."""
    with EphemeralPostgres() as engine:
        install_supabase_auth_stub(engine)
        apply_sql(engine, read_repo_sql(COLLECTIONS_MIGRATION))
        apply_sql(engine, read_repo_sql(POLICIES_FILE))
        apply_sql(engine, SEED)
        yield engine


def member(engine: Engine, email: str, **extra: object) -> TamperedClient:
    return TamperedClient(engine, claims=supabase_claims(email, **extra))


def service(engine: Engine) -> TamperedClient:
    return TamperedClient(engine, claims={}, role="service_role")


# --- positive controls (so the deny tests below cannot pass vacuously) -------


def test_a_member_reads_their_own_workspace(backend: Engine) -> None:
    alice = member(backend, "alice@acme.dev")
    assert [row.id for row in alice.fetch_all("select id from workspaces")] == ["ws_a"]
    assert len(alice.fetch_all("select id from members")) == 4  # incl. the revoked row
    assert {row.id for row in alice.fetch_all("select id from projects")} == {"prj_a", "prj_a2"}
    for table, probe in [
        ("tickets", "tkt_a"),
        ("comments", "cmt_a"),
        ("audit_events", "aud_a"),
        ("agent_proposals", "prop_a"),
    ]:
        assert [row.id for row in alice.fetch_all(f"select id from {table}")] == [probe]


def test_a_member_writes_in_their_own_workspace(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    created = bob.attempt(
        "insert into tickets (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, project_id, title, description, status,"
        " priority, labels, acceptance_criteria, lifecycle_stage) values"
        f" ('tkt_new', {ENVELOPE}, 'prj_a', 'mine', '', 'todo', 'medium', '[]'::json,"
        " '', 'intake')"
    )
    assert created.ok and created.rowcount == 1
    updated = bob.attempt("update tickets set title = 'renamed' where id = 'tkt_a'")
    assert updated.ok and updated.rowcount == 1


# --- the tampered client (sprint risk note: this MUST fail to read) ----------


def test_tampered_client_cannot_read_another_workspace(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    for table, probe in CROSS_WORKSPACE_READS:
        assert bob.fetch_all(probe) == [], f"{table}: workspace B leaked to a member of A"


def test_tampered_client_cannot_write_another_workspace(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    attempts = {
        "insert ticket into B": bob.attempt(
            "insert into tickets (id, created_at, updated_at, actor_seq, visibility,"
            " hosting_mode, retention_policy, project_id, title, description, status,"
            " priority, labels, acceptance_criteria, lifecycle_stage) values"
            f" ('tkt_evil', {ENVELOPE}, 'prj_b', 'evil', '', 'todo', 'medium', '[]'::json,"
            " '', 'intake')"
        ),
        "update B ticket": bob.attempt("update tickets set title = 'pwned' where id = 'tkt_b'"),
        "delete B ticket": bob.attempt("delete from tickets where id = 'tkt_b'"),
        "insert project into B": bob.attempt(
            "insert into projects (id, created_at, updated_at, actor_seq, visibility,"
            " hosting_mode, retention_policy, workspace_id, name, goal, scope, status)"
            f" values ('prj_evil', {ENVELOPE}, 'ws_b', 'evil', '', '', 'active')"
        ),
        "rename workspace B": bob.attempt("update workspaces set name = 'pwned' where id = 'ws_b'"),
        "join workspace B": bob.attempt(
            "insert into members (id, created_at, updated_at, actor_seq, visibility,"
            " hosting_mode, retention_policy, workspace_id, email, role, status) values"
            f" ('mbr_evil', {ENVELOPE}, 'ws_b', 'bob@acme.dev', 'Owner', 'active')"
        ),
    }
    for what, attempt in attempts.items():
        assert attempt.denied, f"{what}: a member of A mutated workspace B"
    untouched = service(backend).fetch_all("select title from tickets where id = 'tkt_b'")
    assert untouched[0].title == "B ticket"


def test_forged_role_claim_does_not_escalate(backend: Engine) -> None:
    """Claims are attacker-controlled text; the Postgres role is what counts."""
    forger = TamperedClient(
        backend,
        claims=supabase_claims("bob@acme.dev", role="service_role"),
        role="authenticated",
    )
    assert forger.fetch_all("select id from tickets where id = 'tkt_b'") == []


def test_anon_role_gets_nothing(backend: Engine) -> None:
    """anon holds no grants at all: deny is an error, not even an empty list."""
    anon = TamperedClient(backend, claims={}, role="anon")
    for table in COLLECTION_TABLES:
        attempt = anon.attempt(f"select id from {table}")
        assert not attempt.ok, f"{table}: anon was allowed to select"
        assert "permission denied" in attempt.error


def test_revoked_member_loses_all_access(backend: Engine) -> None:
    revoked = member(backend, "rev@acme.dev")
    for table in COLLECTION_TABLES:
        assert revoked.fetch_all(f"select id from {table}") == [], (
            f"{table}: a revoked member still reads workspace data"
        )


def test_unknown_email_sees_nothing(backend: Engine) -> None:
    stranger = member(backend, "stranger@nowhere.dev")
    for table in COLLECTION_TABLES:
        assert stranger.fetch_all(f"select id from {table}") == []


def test_a_ticket_cannot_be_moved_to_another_workspace(backend: Engine) -> None:
    """Security review F1: UPDATE's WITH CHECK must validate the new project."""
    bob = member(backend, "bob@acme.dev")
    within = bob.attempt("update tickets set project_id = 'prj_a2' where id = 'tkt_a'")
    assert within.ok and within.rowcount == 1  # positive control: same workspace
    across = bob.attempt("update tickets set project_id = 'prj_b' where id = 'tkt_a'")
    assert across.denied, "a ticket was moved into another workspace's project"
    home = service(backend).fetch_all("select project_id from tickets where id = 'tkt_a'")
    assert home[0].project_id == "prj_a2"


def test_a_maintainer_cannot_touch_the_owner_tier(backend: Engine) -> None:
    """Security review F2: no Owner lockout / self-promotion by a Maintainer."""
    maintainer = member(backend, "max@acme.dev")
    invite = maintainer.attempt(  # positive control: admin powers do work
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_eve', {ENVELOPE}, 'ws_a', 'eve@acme.dev', 'Member', 'invited')"
    )
    assert invite.ok and invite.rowcount == 1
    lockout = maintainer.attempt("update members set status = 'revoked' where id = 'mbr_alice'")
    assert lockout.denied, "a Maintainer revoked the Owner"
    promote = maintainer.attempt("update members set role = 'Owner' where id = 'mbr_max'")
    assert promote.denied, "a Maintainer promoted themselves to Owner"
    puppet = maintainer.attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_puppet', {ENVELOPE}, 'ws_a', 'puppet@acme.dev', 'Owner', 'active')"
    )
    assert not puppet.ok, "a Maintainer minted a new Owner"
    owner = service(backend).fetch_all("select role, status from members where id = 'mbr_alice'")
    assert (owner[0].role, owner[0].status) == ("Owner", "active")


def test_an_owner_manages_the_owner_tier(backend: Engine) -> None:
    alice = member(backend, "alice@acme.dev")
    promote = alice.attempt("update members set role = 'Owner' where id = 'mbr_max'")
    assert promote.ok and promote.rowcount == 1


def test_authorship_cannot_be_forged(backend: Engine) -> None:
    """Security review F3: comments/proposals are written AS yourself."""
    bob = member(backend, "bob@acme.dev")
    forged_comment = bob.attempt(
        "insert into comments (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, ticket_id, author_actor_id, body) values"
        f" ('cmt_forged', {ENVELOPE}, 'tkt_a', 'mbr_alice', 'as alice')"
    )
    assert not forged_comment.ok, "bob commented in alice's name"
    own_comment = bob.attempt(
        "insert into comments (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, ticket_id, author_actor_id, body) values"
        f" ('cmt_own', {ENVELOPE}, 'tkt_a', 'mbr_bob', 'as myself')"
    )
    assert own_comment.ok
    forged_proposal = bob.attempt(
        "insert into agent_proposals (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, ticket_id, proposer_id, diff, status) values"
        f" ('prop_forged', {ENVELOPE}, 'tkt_a', 'mbr_alice', '{{}}'::json, 'pending')"
    )
    assert not forged_proposal.ok, "bob proposed in alice's name"


def test_comments_are_edited_only_by_their_author(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    edit_alices = bob.attempt("update comments set body = 'rewritten' where id = 'cmt_a'")
    assert edit_alices.denied, "bob rewrote alice's comment"
    bob.attempt(
        "insert into comments (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, ticket_id, author_actor_id, body) values"
        f" ('cmt_bob', {ENVELOPE}, 'tkt_a', 'mbr_bob', 'draft')"
    )
    edit_own = bob.attempt("update comments set body = 'final' where id = 'cmt_bob'")
    assert edit_own.ok and edit_own.rowcount == 1


def test_a_jwt_without_an_email_claim_matches_nothing(backend: Engine) -> None:
    """Security review F4: an absent email claim must never link to a member."""
    planted = service(backend).attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_blank', {ENVELOPE}, 'ws_a', '', 'Member', 'active')"
    )
    assert planted.ok  # a hostile/buggy row, written past RLS by the backend
    ghost = TamperedClient(
        backend,
        claims={"sub": "00000000-0000-0000-0000-00000000dead", "role": "authenticated"},
    )
    for table in COLLECTION_TABLES:
        assert ghost.fetch_all(f"select id from {table}") == [], (
            f"{table}: an emailless JWT was linked to a member"
        )


# --- audit append-only, enforced by the database (E07's rule, server side) ---


def test_audit_is_append_only_even_for_the_owner(backend: Engine) -> None:
    alice = member(backend, "alice@acme.dev")  # Owner of workspace A
    tamper = alice.attempt("update audit_events set action = 'cover-up' where id = 'aud_a'")
    assert not tamper.ok and "permission denied" in tamper.error
    erase = alice.attempt("delete from audit_events where id = 'aud_a'")
    assert not erase.ok and "permission denied" in erase.error
    intact = service(backend).fetch_all("select action from audit_events where id = 'aud_a'")
    assert intact[0].action == "ticket.update"


def test_audit_events_are_written_only_as_yourself(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    forged = bob.attempt(
        "insert into audit_events (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, actor_id, action, source) values"
        f" ('aud_forged', {ENVELOPE}, 'mbr_alice', 'ticket.update', 'app')"
    )
    assert not forged.ok, "bob wrote an audit event in alice's name"
    own = bob.attempt(
        "insert into audit_events (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, actor_id, action, source) values"
        f" ('aud_own', {ENVELOPE}, 'mbr_bob', 'ticket.update', 'app')"
    )
    assert own.ok and own.rowcount == 1


# --- tokens: by member, with workspace admins managing (E06 semantics) -------


def test_tokens_are_scoped_to_their_member(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    assert [r.id for r in bob.fetch_all("select id from tokens")] == ["tok_bob"]
    grab = bob.attempt("update tokens set revoked_at = now() where id = 'tok_alice'")
    assert grab.denied, "bob revoked alice's token"


def test_workspace_admins_manage_all_workspace_tokens(backend: Engine) -> None:
    alice = member(backend, "alice@acme.dev")  # Owner = admin
    visible = {r.id for r in alice.fetch_all("select id from tokens")}
    assert visible == {"tok_alice", "tok_bob"}  # both of A, never cher's
    rotate = alice.attempt("update tokens set revoked_at = now() where id = 'tok_bob'")
    assert rotate.ok and rotate.rowcount == 1


def test_plain_members_cannot_invite_admins_can(backend: Engine) -> None:
    bob = member(backend, "bob@acme.dev")
    invite = bob.attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_eve', {ENVELOPE}, 'ws_a', 'eve@acme.dev', 'Member', 'invited')"
    )
    assert not invite.ok, "a plain Member invited someone"
    alice = member(backend, "alice@acme.dev")
    invite = alice.attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_eve', {ENVELOPE}, 'ws_a', 'eve@acme.dev', 'Member', 'invited')"
    )
    assert invite.ok and invite.rowcount == 1


# --- workspace bootstrap ------------------------------------------------------


def test_a_new_user_bootstraps_their_own_workspace(backend: Engine) -> None:
    dana = member(backend, "dana@new.dev")
    ws = dana.attempt(
        "insert into workspaces (id, created_at, updated_at, actor_seq, visibility,"
        f" hosting_mode, retention_policy, name) values ('ws_d', {ENVELOPE}, 'Dana Co')"
    )
    assert ws.ok
    owner = dana.attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_dana', {ENVELOPE}, 'ws_d', 'dana@new.dev', 'Owner', 'active')"
    )
    assert owner.ok
    assert [r.id for r in dana.fetch_all("select id from workspaces")] == ["ws_d"]


def test_bootstrap_cannot_claim_an_existing_workspace(backend: Engine) -> None:
    dana = member(backend, "dana@new.dev")
    takeover = dana.attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_dana', {ENVELOPE}, 'ws_a', 'dana@new.dev', 'Owner', 'active')"
    )
    assert not takeover.ok, "a stranger made themselves Owner of an existing workspace"


def test_bootstrap_cannot_mint_someone_else_as_owner(backend: Engine) -> None:
    eve = member(backend, "eve@new.dev")
    assert eve.attempt(
        "insert into workspaces (id, created_at, updated_at, actor_seq, visibility,"
        f" hosting_mode, retention_policy, name) values ('ws_e', {ENVELOPE}, 'Eve Co')"
    ).ok
    minted = eve.attempt(
        "insert into members (id, created_at, updated_at, actor_seq, visibility,"
        " hosting_mode, retention_policy, workspace_id, email, role, status) values"
        f" ('mbr_planted', {ENVELOPE}, 'ws_e', 'alice@acme.dev', 'Owner', 'active')"
    )
    assert not minted.ok, "the bootstrap arm minted an Owner row for another email"


# --- service role + schema parity --------------------------------------------


def test_service_role_bypasses_rls(backend: Engine) -> None:
    """The backend side (NFR-E24-1 is why this role never reaches a client)."""
    rows = service(backend).fetch_all("select id from tickets order by id")
    assert [r.id for r in rows] == ["tkt_a", "tkt_b"]


def test_checked_in_migration_builds_the_models_schema() -> None:
    """Live D-07 parity: the SQL file and the metadata build the same schema."""
    with EphemeralPostgres() as from_file:
        apply_sql(from_file, read_repo_sql(COLLECTIONS_MIGRATION))
        file_structure = reflect_structure(from_file)
    with EphemeralPostgres() as from_models:
        SQLModel.metadata.create_all(from_models)
        model_structure = {
            name: shape
            for name, shape in reflect_structure(from_models).items()
            if name in COLLECTION_TABLES
        }
    assert file_structure == model_structure


def test_grants_match_the_documented_ceiling(backend: Engine) -> None:
    """Supabase auto-grants ALL on new tables; the policies must strip it.

    The stub models Supabase's default privileges, so this pins the belt
    (grants), not just the suspenders (policies): anon holds nothing anywhere,
    and audit_events is append-only at the grant layer too.
    """
    with backend.connect() as conn:
        rows = conn.execute(
            text(
                "select grantee, table_name, privilege_type"
                " from information_schema.role_table_grants"
                " where table_schema = 'public' and grantee in ('anon', 'authenticated')"
                "   and table_name = any(:tables)"
            ),
            {"tables": COLLECTION_TABLES},
        ).all()
    anon_grants = [r for r in rows if r.grantee == "anon"]
    assert anon_grants == [], f"anon still holds grants: {anon_grants}"
    audit_grants = {
        r.privilege_type
        for r in rows
        if r.grantee == "authenticated" and r.table_name == "audit_events"
    }
    assert audit_grants == {"SELECT", "INSERT"}, (
        f"audit_events must be append-only at the grant layer too, got {audit_grants}"
    )


def test_policies_file_reapplies_cleanly(backend: Engine) -> None:
    """The maintainer re-applies the file after updates; it must be idempotent."""
    apply_sql(backend, read_repo_sql(POLICIES_FILE))
    alice = member(backend, "alice@acme.dev")
    assert [row.id for row in alice.fetch_all("select id from workspaces")] == ["ws_a"]
    assert (
        member(backend, "bob@acme.dev").fetch_all("select id from tickets where id = 'tkt_b'") == []
    )


def test_rls_is_enabled_on_every_collection(backend: Engine) -> None:
    with backend.connect() as conn:
        rows = conn.execute(
            text("select relname, relrowsecurity from pg_class where relname = any(:tables)"),
            {"tables": COLLECTION_TABLES},
        )
        enabled = {row.relname: row.relrowsecurity for row in rows}
    assert enabled == dict.fromkeys(COLLECTION_TABLES, True)
