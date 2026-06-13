"""E23-T1 — the portable export bundle producer (MOD-23, Portability profile).

Pins the locked layout, the signed manifest + per-file hashes, the
content-addressed blobs, the full-bundle round-trip-read (events re-fold to the
snapshot), incremental ``?since``, byte-determinism, and the HTTP route.
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import (
    IdentityService,
    TokenVerifier,
    ensure_device,
    ensure_member_grant,
)
from kantaq_core.identity.devices import device_private_key
from kantaq_core.tracker import LocalBlobStore, TrackerService
from kantaq_db import new_ulid
from kantaq_db.models import Member, Ticket
from kantaq_protocol import Event, decode, public_key_of, verify_bytes
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_runtime.export import MANIFEST_SIGNING_DOMAIN, build_bundle
from kantaq_sync_engine import EventLogSink, EventSigner, fold_events, insert_event
from kantaq_sync_engine.snapshot import parse_snapshot
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.keychain import FakeKeychain

FIXED_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def blob_store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def _seed(engine: Engine, keychain: FakeKeychain) -> tuple[str, str, str]:
    """Bootstrap owner + device + a signing tracker write; return
    (owner_id, workspace_id, device_public_key)."""
    with Session(engine) as session:
        owner = IdentityService(session).bootstrap_owner()
        assert owner is not None
        owner_id = owner.member_id
        workspace_id = session.get(Member, owner_id).workspace_id  # type: ignore[union-attr]
        ensure_device(session, keychain, member_id=owner_id)
        grant = ensure_member_grant(session, keychain, owner_id)
        device_key = device_private_key(keychain)
        assert device_key is not None
        signer = EventSigner(private_key=device_key, policy_ref=grant.id)
        service = TrackerService(
            session,
            actor_id=owner_id,
            source="app",
            sink=EventLogSink(session, owner_id, signer=signer),
        )
        project = service.create_project(workspace_id=workspace_id, name="Skeleton")
        service.create_ticket(project_id=project.id, title="Walk end to end")
        session.commit()
    return owner_id, workspace_id, public_key_of(device_key)


def _members(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


def _build(
    engine: Engine, keychain: FakeKeychain, blob_store: LocalBlobStore, **kw: object
) -> bytes:
    with Session(engine) as session:
        return build_bundle(
            session,
            blob_store=blob_store,
            now=kw.pop("now", FIXED_NOW),  # type: ignore[arg-type]
            device_key=device_private_key(keychain),
            **kw,  # type: ignore[arg-type]
        )


def test_bundle_carries_every_required_part(engine: Engine, blob_store: LocalBlobStore) -> None:
    keychain = FakeKeychain()
    _seed(engine, keychain)
    files = _members(_build(engine, keychain, blob_store))

    assert "manifest.json" in files
    assert "team_manifest.json" in files
    assert "actors.ndjson" in files
    assert "grants.ndjson" in files
    assert "collections/tickets/snapshot.ndjson" in files
    assert "collections/tickets/events.ndjson" in files
    assert "audit/anchors.json" in files
    assert "audit/policies.json" in files
    # Every syncable collection gets a (possibly empty) snapshot + events pair.
    assert "collections/workspaces/events.ndjson" in files


def test_manifest_signature_and_per_file_hashes_verify(
    engine: Engine, blob_store: LocalBlobStore
) -> None:
    keychain = FakeKeychain()
    _, _, device_pub = _seed(engine, keychain)
    files = _members(_build(engine, keychain, blob_store))

    manifest = json.loads(files["manifest.json"])
    # The signed root of trust verifies against the exporting device key.
    core = {k: v for k, v in manifest.items() if k != "signature"}
    from kantaq_protocol import canonicalize

    assert verify_bytes(
        MANIFEST_SIGNING_DOMAIN + canonicalize(core), manifest["signature"], device_pub
    )
    # Every recorded hash matches the file's actual bytes (no silent corruption).
    import hashlib

    for path, meta in manifest["files"].items():
        assert path != "manifest.json"  # the manifest never hashes itself
        assert hashlib.sha256(files[path]).hexdigest() == meta["sha256"]


def test_full_bundle_events_refold_to_the_snapshot(
    engine: Engine, blob_store: LocalBlobStore
) -> None:
    keychain = FakeKeychain()
    _seed(engine, keychain)
    files = _members(_build(engine, keychain, blob_store))

    events = [
        decode(line.encode("utf-8"))
        for line in files["collections/tickets/events.ndjson"].decode().splitlines()
        if line.strip()
    ]
    refolded = fold_events(events)
    snapshot = parse_snapshot(files["collections/tickets/snapshot.ndjson"].decode())
    assert refolded == snapshot
    assert snapshot  # there is a ticket to compare


def test_incremental_since_returns_only_the_committed_delta(
    engine: Engine, blob_store: LocalBlobStore
) -> None:
    keychain = FakeKeychain()
    _seed(engine, keychain)
    # Two committed events on one entity (rev 1, 2), as if pulled from a backend.
    with Session(engine) as session:
        for seq, rev in ((10, 1), (11, 2)):
            insert_event(
                session,
                Event(
                    event_id=new_ulid(),
                    collection="comments",
                    entity_id="cmt_x".ljust(26, "0"),
                    actor_id="mbr_x".ljust(26, "0"),
                    actor_seq=seq,
                    payload={"body": f"v{seq}"},
                ),
                committed_rev=rev,
            )
        session.commit()

    full = _members(_build(engine, keychain, blob_store))
    incremental = _members(_build(engine, keychain, blob_store, since=1))
    full_events = full["collections/comments/events.ndjson"].decode().splitlines()
    delta_events = incremental["collections/comments/events.ndjson"].decode().splitlines()
    assert len(full_events) == 2
    assert len(delta_events) == 1  # only committed_rev > 1
    assert json.loads(incremental["manifest.json"])["since"] == 1


def test_blob_is_exported_and_content_addressed(engine: Engine, blob_store: LocalBlobStore) -> None:
    keychain = FakeKeychain()
    _seed(engine, keychain)
    ref = blob_store.store(b"attachment bytes", filename="notes.txt", media_type="text/plain")
    with Session(engine) as session:
        ticket = session.exec(select(Ticket)).first()
        assert ticket is not None
        ticket.attachments = [ref.to_json()]
        session.add(ticket)
        session.commit()

    files = _members(_build(engine, keychain, blob_store))
    assert files[f"blobs/data/{ref.blob_id}"] == b"attachment bytes"
    manifest = json.loads(files["blobs/manifest.json"])
    assert manifest[ref.blob_id]["sha256"] == ref.blob_id
    assert manifest[ref.blob_id]["filename"] == "notes.txt"


def test_bundle_is_byte_deterministic(engine: Engine, blob_store: LocalBlobStore) -> None:
    keychain = FakeKeychain()
    _seed(engine, keychain)
    first = _build(engine, keychain, blob_store)
    second = _build(engine, keychain, blob_store)
    assert first == second  # same store + same now → identical bytes


def test_export_route_returns_a_bundle(engine: Engine, tmp_path: Path) -> None:
    # The route resolves its keychain from settings; seed the device into that path.
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))
    from kantaq_runtime.auth import keychain_for

    real_keychain = keychain_for(settings)
    with Session(engine) as session:
        owner = IdentityService(session).bootstrap_owner()
        assert owner is not None
        ensure_device(session, real_keychain, member_id=owner.member_id)
        session.commit()
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    app = create_app(settings=settings, engine=engine, verifier=verifier)
    with TestClient(app) as client:
        response = client.post("/v1/export", headers={"Authorization": f"Bearer {owner.plaintext}"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/gzip"
    files = _members(response.content)
    assert "manifest.json" in files
