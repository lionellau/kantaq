"""The adapter against real Postgres + RLS, via FakePostgREST (E24-T4).

This is the live half of the backend-port contract: the same
``SupabaseSyncBackend`` calls the runtime makes, answered the way PostgREST
answers them — claims applied, role set, real SQL, real RLS, real
identity-assigned revisions. It pins the MOD-05 Sprint-2 required test (sync
last-writer-wins by commit order, the FakeBackend-pinned fold shape) and runs
the two-replica simulator over the real adapter, closing the loop the E04
suite proved against FakeBackend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from kantaq_backend_supabase.sync import SupabaseSyncBackend, SyncBackendError
from kantaq_sync_engine import compose_snapshot
from kantaq_sync_engine.events import Event
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.postgrest import FakePostgREST, encode_test_jwt
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, make_replica
from kantaq_test_harness.rls import supabase_claims

ANON_KEY = "anon-key-for-tests"


def _adapter(
    engine: Engine,
    email: str,
    workspace_id: str,
    *,
    now: Any = None,
    claims: dict[str, Any] | None = None,
    refresh: Any = None,
) -> SupabaseSyncBackend:
    fake = FakePostgREST(engine, now=now)
    token = encode_test_jwt(claims if claims is not None else supabase_claims(email))
    return SupabaseSyncBackend(
        fake.base_url,
        ANON_KEY,
        workspace_id=workspace_id,
        access_token=lambda: token,
        refresh=refresh,
        client=fake.client(),
    )


def _event(seq: int, *, actor: str, entity: str = "tkt_x", **payload: Any) -> Event:
    return Event(
        # Unique per (actor, seq), like the real ULIDs are unique per event.
        event_id=f"evt_{actor[4:9]}_{seq:09d}",
        collection="tickets",
        entity_id=entity,
        actor_id=actor,
        actor_seq=seq,
        payload=payload or {"title": f"v{seq}"},
    )


# ----------------------------------------------------------- port contract


def test_push_commits_in_order_and_repush_is_idempotent(sync_pg: Engine) -> None:
    backend = _adapter(sync_pg, "bob@acme.dev", "ws_a")
    events = [_event(1, actor="mbr_bob"), _event(2, actor="mbr_bob")]

    committed = backend.push(events)
    assert [entry.event.actor_seq for entry in committed] == [1, 2]
    assert committed[0].revision < committed[1].revision  # commit order, monotonic

    again = backend.push(events)
    assert again == []  # NFR-E04-2: the duplicates dropped silently

    partial = backend.push([events[1], _event(3, actor="mbr_bob")])
    assert [entry.event.actor_seq for entry in partial] == [3]  # only the new one


def test_pull_returns_commit_order_since_the_cursor(sync_pg: Engine) -> None:
    backend = _adapter(sync_pg, "bob@acme.dev", "ws_a")
    committed = backend.push([_event(i, actor="mbr_bob") for i in (1, 2, 3)])

    everything = backend.pull(collection="tickets")
    revisions = [entry.revision for entry in everything]
    assert revisions == sorted(revisions)
    assert {entry.event.event_id for entry in committed} <= {
        entry.event.event_id for entry in everything
    }

    later = backend.pull(collection="tickets", since=committed[0].revision)
    assert committed[0].event.event_id not in {entry.event.event_id for entry in later}


def test_pull_pages_through_a_large_batch(sync_pg: Engine) -> None:
    backend = _adapter(sync_pg, "bob@acme.dev", "ws_a")
    backend.push([_event(i, actor="mbr_bob") for i in range(1, 8)])

    fake = FakePostgREST(sync_pg)
    paged = SupabaseSyncBackend(
        fake.base_url,
        ANON_KEY,
        workspace_id="ws_a",
        access_token=lambda: encode_test_jwt(supabase_claims("bob@acme.dev")),
        client=fake.client(),
        page_size=3,
    )
    entries = paged.pull(collection="tickets")
    assert len(entries) == 8  # 7 pushed + the seed event, across 3 pages
    revisions = [entry.revision for entry in entries]
    assert revisions == sorted(revisions)


def test_lww_by_commit_order_matches_the_fakebackend_contract(sync_pg: Engine) -> None:
    """The MOD-05 required test: LWW by commit order, FakeBackend-pinned shape."""
    alice = _adapter(sync_pg, "alice@acme.dev", "ws_a")
    bob = _adapter(sync_pg, "bob@acme.dev", "ws_a")

    alice.push([_event(11, actor="mbr_alice", entity="tkt_lww", title="alice wrote first")])
    bob.push([_event(11, actor="mbr_bob", entity="tkt_lww", title="bob committed later")])

    snapshot = alice.snapshot("tickets")
    assert snapshot["tkt_lww"]["title"] == "bob committed later"

    # The same committed stream fed to FakeBackend folds identically.
    fake = FakeBackend()
    fake.push([entry.event for entry in alice.pull(collection="tickets")])
    assert fake.snapshot("tickets") == snapshot


# ------------------------------------------------------------ RLS through it


def test_pull_never_leaks_another_workspace(sync_pg: Engine) -> None:
    bob = _adapter(sync_pg, "bob@acme.dev", "ws_a")
    entities = {entry.event.entity_id for entry in bob.pull()}
    assert "tkt_b" not in entities, "workspace B's events leaked through the adapter"

    # Even pointing the adapter straight at workspace B: RLS filters to nothing.
    nosy = _adapter(sync_pg, "bob@acme.dev", "ws_b")
    assert nosy.pull() == []


def test_push_into_a_foreign_workspace_is_denied(sync_pg: Engine) -> None:
    nosy = _adapter(sync_pg, "bob@acme.dev", "ws_b")
    with pytest.raises(SyncBackendError) as excinfo:
        nosy.push([_event(21, actor="mbr_bob")])
    assert excinfo.value.status_code == 403


def test_forged_actor_is_denied(sync_pg: Engine) -> None:
    bob = _adapter(sync_pg, "bob@acme.dev", "ws_a")
    with pytest.raises(SyncBackendError) as excinfo:
        bob.push([_event(22, actor="mbr_alice")])
    assert excinfo.value.status_code == 403


def test_an_expired_jwt_refreshes_and_retries(sync_pg: Engine) -> None:
    clock = FakeClock()
    epoch = clock.now().timestamp()
    fake = FakePostgREST(sync_pg, now=lambda: clock.now().timestamp())
    expired = encode_test_jwt(supabase_claims("bob@acme.dev", exp=epoch - 60))
    fresh = encode_test_jwt(supabase_claims("bob@acme.dev", exp=epoch + 3600))
    refreshed: list[str] = []

    def refresh() -> str:
        refreshed.append(fresh)
        return fresh

    backend = SupabaseSyncBackend(
        fake.base_url,
        ANON_KEY,
        workspace_id="ws_a",
        access_token=lambda: expired,
        refresh=refresh,
        client=fake.client(),
    )
    entries = backend.pull(collection="tickets")
    assert refreshed and [entry.event.entity_id for entry in entries] == ["tkt_a"]


# ------------------------------------------------- two replicas, real backend


@pytest.fixture
def replicas(sync_pg: Engine, tmp_path: Path) -> tuple[Replica, Replica]:
    """Two members' runtimes-in-miniature on the real Supabase-shaped backend."""
    a = make_replica(tmp_path, "a", _adapter(sync_pg, "a@team.dev", WORKSPACE_ID))
    b = make_replica(tmp_path, "b", _adapter(sync_pg, "b@team.dev", WORKSPACE_ID))
    return a, b


def test_two_runtimes_converge_through_the_live_backend(
    replicas: tuple[Replica, Replica],
) -> None:
    """Exit criterion #1's engine half: A's work reaches B via Supabase."""
    a, b = replicas
    with a.session() as session:
        service = a.service(session)
        project = service.create_project(workspace_id=WORKSPACE_ID, name="Skeleton")
        service.create_ticket(project_id=project.id, title="Walk end to end")

    a.sync.push()
    b.sync.pull()
    a.sync.pull()

    with a.session() as sa, b.session() as sb:
        for collection in ("projects", "tickets"):
            assert compose_snapshot(sa, collection) == compose_snapshot(sb, collection)


def test_d05_out_of_order_arrival_converges(replicas: tuple[Replica, Replica]) -> None:
    """The hard case: a later-committed local write applied optimistically
    before an earlier-committed remote write arrives. Commit order must win
    on the real backend exactly as it does on FakeBackend."""
    a, b = replicas
    with a.session() as session:
        ticket = a.service(session).create_ticket(
            project_id=a.service(session).create_project(workspace_id=WORKSPACE_ID, name="P").id,
            title="original",
        )
        ticket_id = ticket.id
    a.sync.push()
    b.sync.pull()

    # B edits and commits FIRST; A edits later locally (optimistic) but
    # pushes SECOND, so A's write holds the higher revision.
    with b.session() as session:
        b.service(session).update_ticket(ticket_id, {"title": "bob first"})
    b.sync.push()
    with a.session() as session:
        a.service(session).update_ticket(ticket_id, {"title": "alice second"})
    a.sync.push()

    for replica in (a, b):
        replica.sync.pull()

    with a.session() as sa, b.session() as sb:
        assert compose_snapshot(sa, "tickets") == compose_snapshot(sb, "tickets")
        title = sa.exec(_title_query(ticket_id)).one()
        assert title == "alice second"


def _title_query(ticket_id: str) -> Any:
    from sqlmodel import select

    from kantaq_db import Ticket

    return select(Ticket.title).where(Ticket.id == ticket_id)


def test_sync_audits_the_original_actor(replicas: tuple[Replica, Replica]) -> None:
    """Every ingested remote event writes one audit row, source="sync"."""
    a, b = replicas
    with a.session() as session:
        a.service(session).create_project(workspace_id=WORKSPACE_ID, name="Audited")
    a.sync.push()
    b.sync.pull()

    from sqlalchemy import text

    with b.session() as session:
        rows = (
            session.connection()
            .execute(
                text("select actor_id, source from audit_events where action = 'project.sync'")
            )
            .all()
        )
    assert rows and all((row.actor_id, row.source) == (a.actor_id, "sync") for row in rows)
