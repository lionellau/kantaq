"""E23-T2 — the export → import → re-export round-trip (MOD-23, Portability).

Proves FR-E23-2 on a synthetic, de-identified fixture workspace: a bundle
imported into a *fresh* runtime re-exports to byte-identical event logs and
identical snapshots, with every blob still content-addressed. The fixture is
built in-test (deterministic ULIDs are avoided in assertions; no real dogfood
data is committed — the manual procedure in docs covers the real workspace).

Also pins the importer's fail-closed behaviour: a corrupted file fails its
manifest hash check, and a tampered-but-rehashed signed event fails ingest
verification — the importer never half-imports.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from kantaq_core.identity import IdentityService, ensure_device, ensure_member_grant
from kantaq_core.identity.devices import device_private_key
from kantaq_core.tracker import LocalBlobStore, TrackerService
from kantaq_db.models import Member
from kantaq_runtime.export import build_bundle
from kantaq_runtime.import_bundle import BundleImportError, import_bundle
from kantaq_sync_engine import SYNCABLE_MODELS, EventLogSink, EventSigner
from kantaq_test_harness.keychain import FakeKeychain

FIXED_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _fresh_engine() -> Engine:
    engine = create_engine("sqlite://")  # in-memory; the round-trip is in the bundle
    SQLModel.metadata.create_all(engine)
    return engine


def _members(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


def _retar(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def _seed_source(engine: Engine, keychain: FakeKeychain, blob_store: LocalBlobStore) -> None:
    """A synthetic signed workspace: project, a ticket with two events, a
    comment, and an attached blob — enough to exercise ordering + blobs."""
    with Session(engine) as session:
        owner = IdentityService(session).bootstrap_owner()
        assert owner is not None
        workspace_id = session.get(Member, owner.member_id).workspace_id  # type: ignore[union-attr]
        ensure_device(session, keychain, member_id=owner.member_id)
        grant = ensure_member_grant(session, keychain, owner.member_id)
        signer = EventSigner(private_key=device_private_key(keychain), policy_ref=grant.id)  # type: ignore[arg-type]
        service = TrackerService(
            session,
            actor_id=owner.member_id,
            source="app",
            sink=EventLogSink(session, owner.member_id, signer=signer),
        )
        project = service.create_project(workspace_id=workspace_id, name="Fixture Project")
        ticket = service.create_ticket(project_id=project.id, title="Round-trip me")
        service.update_ticket(ticket.id, {"status": "doing"})  # a second tickets event
        service.add_comment(ticket.id, "a synthetic comment")
        ref = blob_store.store(
            b"fixture attachment bytes", filename="notes.txt", media_type="text/plain"
        )
        service.add_attachment(ticket.id, ref)  # emits a tickets event carrying the attachment
        session.commit()


def _source_bundle(tmp_path: Path) -> bytes:
    engine = _fresh_engine()
    keychain = FakeKeychain()
    blobs = LocalBlobStore(tmp_path / "src-blobs")
    _seed_source(engine, keychain, blobs)
    with Session(engine) as session:
        return build_bundle(
            session, blob_store=blobs, now=FIXED_NOW, device_key=device_private_key(keychain)
        )


def test_export_import_reexport_is_byte_identical(tmp_path: Path) -> None:
    source = _source_bundle(tmp_path)

    # Import into a brand-new runtime (fresh DB + fresh blob store).
    dst_engine = _fresh_engine()
    dst_blobs = LocalBlobStore(tmp_path / "dst-blobs")
    with Session(dst_engine) as session:
        result = import_bundle(source, session=session, blob_store=dst_blobs)
        session.commit()
    assert result.events >= 4  # project + 2 tickets + comment
    assert result.blobs == 1

    # Re-export the imported runtime (no device key here — a fresh runtime has
    # its own; the named guarantee is the event log, not the manifest signature).
    with Session(dst_engine) as session:
        reexport = build_bundle(session, blob_store=dst_blobs, now=FIXED_NOW, device_key=None)

    src_files = _members(source)
    dst_files = _members(reexport)

    # Byte-identical event logs + identical snapshots, per collection (FR-E23-2).
    for collection in sorted(SYNCABLE_MODELS):
        events = f"collections/{collection}/events.ndjson"
        snapshot = f"collections/{collection}/snapshot.ndjson"
        assert dst_files[events] == src_files[events], f"event log drift in {collection}"
        assert dst_files[snapshot] == src_files[snapshot], f"snapshot drift in {collection}"

    # Verified blob hashes: same manifest, bytes present and content-addressed.
    src_blob_manifest = json.loads(src_files["blobs/manifest.json"])
    assert json.loads(dst_files["blobs/manifest.json"]) == src_blob_manifest
    for blob_id, ref in src_blob_manifest.items():
        assert ref["sha256"] == blob_id
        assert hashlib.sha256(dst_files[f"blobs/data/{blob_id}"]).hexdigest() == blob_id


def test_import_refuses_a_corrupted_file(tmp_path: Path) -> None:
    files = _members(_source_bundle(tmp_path))
    files["collections/tickets/snapshot.ndjson"] += b"corruption\n"  # hash no longer matches
    with Session(_fresh_engine()) as session, pytest.raises(BundleImportError):
        import_bundle(_retar(files), session=session, blob_store=LocalBlobStore(tmp_path / "b"))


def test_import_refuses_a_tampered_event(tmp_path: Path) -> None:
    """Tamper a signed event and repair its file hash so integrity passes — the
    signature check at ingest must still refuse it (verified ingestion)."""
    files = _members(_source_bundle(tmp_path))
    path = "collections/tickets/events.ndjson"
    lines = files[path].decode("utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["title"] = "TAMPERED"  # the recorded signature is now stale
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    files[path] = ("\n".join(lines) + "\n").encode("utf-8")

    manifest = json.loads(files["manifest.json"])
    manifest["files"][path]["sha256"] = hashlib.sha256(files[path]).hexdigest()
    manifest["files"][path]["bytes"] = len(files[path])
    files["manifest.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    with Session(_fresh_engine()) as session, pytest.raises(BundleImportError):
        import_bundle(_retar(files), session=session, blob_store=LocalBlobStore(tmp_path / "b"))
