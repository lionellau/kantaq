"""Members distribute as sync events (DEBT-45, docs/design/member-events.md).

The gap the enroll live smoke found (docs/design/enroll.md §9): member rows
were created directly (``seed_member`` on the backend, ``adopt_owner`` on the
replica) and never entered the event log, so a peer replica pulling the
founder's ``devices`` event had no ``members`` row for its ``member_id`` FK —
the second member's first pull died on referential integrity.

Everything here drives REAL runtime replicas and the REAL ASGI sync-server
(the DEBT-42 lesson: no stamped actor ids, no faked joins). Postgres-gated
like the rest of the backend suite.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from kantaq.enroll import (
    EnrollPayload,
    ImportResult,
    import_enrollment,
    provision_enrollment,
)
from kantaq_backend_postgres import SyncServerBackend, create_app, create_schema
from kantaq_db.models import Device, Member, Workspace
from kantaq_db.session import get_engine, sqlite_url
from kantaq_sync_engine import SyncEngine
from kantaq_test_harness.db import EphemeralPostgres

SERVER_URL = "http://testserver"
FOUNDER = "founder@acme.dev"
JOINER = "joiner@acme.dev"


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    if not EphemeralPostgres.available():
        pytest.skip("no KANTAQ_TEST_POSTGRES_URL (the CI Postgres service provides one)")
    with EphemeralPostgres() as engine:
        create_schema(engine)
        yield engine


@pytest.fixture
def app_client(pg_engine: Engine) -> TestClient:
    return TestClient(create_app(pg_engine))


def _payload_for(provisioned: object, *, email: str) -> EnrollPayload:
    now = int(time.time())
    return EnrollPayload(
        backend_mode="postgres",
        hub_url=SERVER_URL,
        hub_token=provisioned.token_plaintext,  # type: ignore[attr-defined]
        member_id=provisioned.member_id,  # type: ignore[attr-defined]
        member_email=email,
        workspace_id=provisioned.workspace_id,  # type: ignore[attr-defined]
        workspace_name=provisioned.workspace_name,  # type: ignore[attr-defined]
        issued_at=now,
        expires_at=now + 3600,
    )


def _import(payload: EnrollPayload, home: Path, app_client: TestClient) -> ImportResult:
    return import_enrollment(
        payload,
        local_db_path=str(home / "data" / "runtime.sqlite"),
        keychain_dir=home / "data" / "keychain",
        env_path=home / ".env",
        client=app_client,
    )


def _replica(home: Path) -> Engine:
    return get_engine(sqlite_url(str(home / "data" / "runtime.sqlite")))


def _sync_once(replica: Engine, token: str, app_client: TestClient) -> object:
    """One push + pull cycle exactly as ``kantaq sync once`` drives it
    (``_postgres_sync_once`` + ``_verifying_backend``, MOD-28): the verifying
    wrapper at its pre-cutover default, then flush the durable outbox through
    the atomic commit path and apply the inbox."""
    from datetime import UTC, datetime

    from kantaq_core.identity import local_grant_index, verification_roots
    from kantaq_sync_engine import VerifyContext, VerifyingBackend

    with Session(replica) as session:
        workspace = session.exec(select(Workspace)).first()
        me = session.exec(
            select(Member).where(Member.status == "active").order_by(col(Member.id))
        ).first()
    assert workspace is not None and me is not None
    hub = SyncServerBackend(SERVER_URL, token, workspace_id=workspace.id, client=app_client)

    def context() -> VerifyContext:
        with Session(replica) as session:
            grants, revoked = local_grant_index(session)
            return VerifyContext(
                roots=verification_roots(session),
                grants=grants,
                now=int(datetime.now(UTC).timestamp()),
                revoked_ids=revoked,
                require_signature=False,  # pre-cutover, the shipped default
                workspace_id=workspace.id,
            )

    backend = VerifyingBackend(hub, context=context)
    engine = SyncEngine(replica, backend, actor_id=me.id, workspace_id=workspace.id)
    engine.flush_outbox()
    return engine.apply_inbox()


def _enrolled(
    pg_engine: Engine, app_client: TestClient, home: Path, *, email: str
) -> tuple[Engine, str, str]:
    """Provision + import one member; returns (replica, member_id, token)."""
    provisioned = provision_enrollment(pg_engine, email=email, workspace="Acme")
    result = _import(_payload_for(provisioned, email=email), home, app_client)
    return _replica(home), result.member_id, provisioned.token_plaintext


# --------------------------------------------------------- the DEBT-45 repro


def test_second_members_pull_materialises_the_founders_identity(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """The exact sequence the live smoke ran: founder enrolls and syncs, a
    second member enrolls and syncs. The joiner's PULL folds the founder's
    ``devices`` event, whose ``member_id`` FK needs the founder's ``members``
    row — distributed as a members event, never as a bare row."""
    founder_home, joiner_home = tmp_path / "founder", tmp_path / "joiner"
    founder_db, founder_id, founder_token = _enrolled(
        pg_engine, app_client, founder_home, email=FOUNDER
    )
    _sync_once(founder_db, founder_token, app_client)  # founder's boot events reach the hub

    joiner_db, joiner_id, joiner_token = _enrolled(pg_engine, app_client, joiner_home, email=JOINER)
    _sync_once(joiner_db, joiner_token, app_client)  # DEBT-45: this pull used to FK-fail

    with Session(joiner_db) as session:
        founder_member = session.get(Member, founder_id)
        assert founder_member is not None, "founder's member row must fold from their event"
        assert founder_member.email == FOUNDER
        assert founder_member.role == "Owner"
        founder_devices = session.exec(select(Device).where(Device.member_id == founder_id)).all()
        assert founder_devices, "founder's device row folds once its member row exists"


def test_the_founder_learns_the_joiner_symmetrically(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """The same gap ran the other way too: the founder pulling the joiner's
    device event had no members row for the joiner. After the joiner's first
    sync, one more founder cycle folds the joiner's member + device rows."""
    founder_home, joiner_home = tmp_path / "founder", tmp_path / "joiner"
    founder_db, _founder_id, founder_token = _enrolled(
        pg_engine, app_client, founder_home, email=FOUNDER
    )
    _sync_once(founder_db, founder_token, app_client)

    joiner_db, joiner_id, joiner_token = _enrolled(pg_engine, app_client, joiner_home, email=JOINER)
    _sync_once(joiner_db, joiner_token, app_client)

    _sync_once(founder_db, founder_token, app_client)
    with Session(founder_db) as session:
        joiner_member = session.get(Member, joiner_id)
        assert joiner_member is not None
        assert joiner_member.email == JOINER
        assert session.exec(select(Device).where(Device.member_id == joiner_id)).all()


def test_a_legacy_stream_with_no_member_event_still_folds_the_device(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """Existing deployments have device events already committed with NO member
    event anywhere in the stream (the pre-fix world). The trust-root ingest
    must satisfy the FK with a placeholder member rather than wedging the pull;
    the founder's next boot backfills the real row (§9 self-heal)."""
    founder_home, joiner_home = tmp_path / "founder", tmp_path / "joiner"
    founder_db, founder_id, founder_token = _enrolled(
        pg_engine, app_client, founder_home, email=FOUNDER
    )
    # Simulate the legacy stream: strip the founder's members event(s) from the
    # pending outbox so only the device event reaches the hub — exactly what a
    # pre-fix runtime pushed.
    from kantaq_db import EventLog

    with Session(founder_db) as session:
        for row in session.exec(select(EventLog).where(EventLog.collection == "members")).all():
            session.delete(row)
        session.commit()
    _sync_once(founder_db, founder_token, app_client)

    joiner_db, _joiner_id, joiner_token = _enrolled(
        pg_engine, app_client, joiner_home, email=JOINER
    )
    _sync_once(joiner_db, joiner_token, app_client)  # must not wedge on the FK

    with Session(joiner_db) as session:
        placeholder = session.get(Member, founder_id)
        assert placeholder is not None, "the ingest satisfies the FK with a placeholder"
        assert placeholder.email == ""  # honest: identity known, profile not yet distributed
        assert session.exec(select(Device).where(Device.member_id == founder_id)).all()
