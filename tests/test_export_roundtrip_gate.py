"""The v0.2 export round-trip CI gate (E27-T5, MOD-15 + MOD-23 / FR-E23-2/3).

Automates the manual ``scripts/roundtrip_check.py`` on a fixture workspace and
extends it two ways the v0.1 check did not cover:

1. **Linear → export → import** is lossless — the gate "tests the importer"
   (E23-T3): a workspace built by importing the synthetic JobWinAI-shaped Linear
   export round-trips byte-identically.
2. **Incremental ``?since=cursor``** round-trips — a delta exported above a
   cursor, applied on top of the base, reproduces the full state.

Each gate ships its failing fixture (MOD-30): a one-byte corruption is caught as
round-trip drift (or refused on import), so the gate is proven to bite.
"""

from __future__ import annotations

import importlib.util
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
from kantaq_runtime.import_bundle import import_bundle
from kantaq_runtime.linear_import import import_linear
from kantaq_sync_engine import EventLogSink, EventSigner, SyncEngine
from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.keychain import FakeKeychain
from kantaq_test_harness.linear_fixture import build_linear_export

# Automate the real manual checker (scripts/ is not an importable package, so
# load it from its path — the gate runs *exactly* the procedure ops use).
_ROUNDTRIP_PATH = Path(__file__).resolve().parents[1] / "scripts" / "roundtrip_check.py"
_spec = importlib.util.spec_from_file_location("kantaq_roundtrip_check", _ROUNDTRIP_PATH)
assert _spec is not None and _spec.loader is not None
_roundtrip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_roundtrip)
check = _roundtrip.check

FIXED_NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)


def _fresh_engine() -> Engine:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine


def _members(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


def _seed_via_linear(engine: Engine, keychain: FakeKeychain) -> EventSigner:
    """A signed workspace built by importing a small synthetic Linear export."""
    with Session(engine) as session:
        owner = IdentityService(session).bootstrap_owner()
        assert owner is not None
        member = session.get(Member, owner.member_id)
        assert member is not None
        ensure_device(session, keychain, member_id=owner.member_id)
        grant = ensure_member_grant(session, keychain, owner.member_id)
        signer = EventSigner(private_key=device_private_key(keychain), policy_ref=grant.id)  # type: ignore[arg-type]
        service = TrackerService(
            session,
            actor_id=owner.member_id,
            source="app",
            sink=EventLogSink(session, owner.member_id, signer=signer),
            now=lambda: FIXED_NOW.replace(tzinfo=None),  # deterministic timestamps
        )
        project = service.create_project(workspace_id=member.workspace_id, name="Imported")
        export = build_linear_export(tickets=12, epics=2, parent_links=6, comments=10)
        import_linear(
            export,
            session=session,
            workspace_id=member.workspace_id,
            project_id=project.id,
            actor_id=owner.member_id,
            source="app",
            signer=signer,
            now=FIXED_NOW.replace(tzinfo=None),
        )
        return signer


def test_linear_seeded_workspace_round_trips_byte_identical(tmp_path: Path) -> None:
    """The gate: a Linear-imported workspace exports → imports → re-exports with
    byte-identical event logs + snapshots + content-addressed blobs."""
    engine = _fresh_engine()
    keychain = FakeKeychain()
    signer = _seed_via_linear(engine, keychain)
    assert signer is not None
    with Session(engine) as session:
        bundle = build_bundle(
            session,
            blob_store=LocalBlobStore(tmp_path / "src"),
            now=FIXED_NOW,
            device_key=device_private_key(keychain),
        )
    rt = tmp_path / "rt"
    rt.mkdir()
    problems = check(bundle, rt)
    assert problems == [], problems


def test_incremental_since_cursor_round_trips(tmp_path: Path) -> None:
    """A delta exported above the base cursor, applied on top of the base, yields
    the same snapshots as a full import (FR-E23-3)."""
    engine = _fresh_engine()
    keychain = FakeKeychain()
    signer = _seed_via_linear(engine, keychain)
    blobs = LocalBlobStore(tmp_path / "src")

    # ?since filters by committed_rev, so the source must have *committed* events:
    # flush the local outbox through a FakeBackend to assign revisions (the sync a
    # real client does before an incremental export).
    backend = FakeBackend()
    with Session(engine) as session:
        owner = IdentityService(session).list_members()[0]
        workspace_id = owner.workspace_id
        actor_id = owner.id
    sync = SyncEngine(engine, backend, actor_id=actor_id, workspace_id=workspace_id)
    sync.flush_outbox()

    with Session(engine) as session:
        base = build_bundle(
            session, blob_store=blobs, now=FIXED_NOW, device_key=device_private_key(keychain)
        )
    base_cursor = json.loads(_members(base)["manifest.json"])["cursor"]

    # A new write above the cursor, then synced (committed_rev > base_cursor).
    with Session(engine) as session:
        service = TrackerService(
            session,
            actor_id=actor_id,
            source="app",
            sink=EventLogSink(session, actor_id, signer=signer),
            now=lambda: FIXED_NOW.replace(tzinfo=None),  # deterministic timestamps
        )
        project = service.create_project(workspace_id=workspace_id, name="Later")
        service.create_ticket(project_id=project.id, title="added after the cursor")
        session.commit()
    sync.flush_outbox()

    with Session(engine) as session:
        delta = build_bundle(
            session,
            blob_store=blobs,
            now=FIXED_NOW,
            device_key=device_private_key(keychain),
            since=base_cursor,
        )
        full = build_bundle(
            session, blob_store=blobs, now=FIXED_NOW, device_key=device_private_key(keychain)
        )

    # The delta carries fewer ticket events than the full export (it's a delta).
    assert len(_members(delta)["collections/tickets/events.ndjson"]) < len(
        _members(full)["collections/tickets/events.ndjson"]
    )

    # Apply base then delta into a fresh runtime; its snapshots match the full.
    fresh = _fresh_engine()
    fresh_blobs = LocalBlobStore(tmp_path / "fresh")
    with Session(fresh) as session:
        import_bundle(base, session=session, blob_store=fresh_blobs)
        import_bundle(delta, session=session, blob_store=fresh_blobs)
        session.commit()
    with Session(fresh) as session:
        reexport = build_bundle(session, blob_store=fresh_blobs, now=FIXED_NOW, device_key=None)

    # Identical *state* (not byte order): base+delta assigns revisions in a
    # different order than a single-pass full import, so snapshot lines can be
    # reordered — the invariant is the same per-entity state, compared as a set.
    def _state(bundle: bytes) -> set[bytes]:
        lines: set[bytes] = set()
        for name, data in _members(bundle).items():
            if name.endswith("snapshot.ndjson"):
                lines.update(line for line in data.splitlines() if line)
        return lines

    assert _state(full) == _state(reexport)  # identical state after the incremental apply


def test_roundtrip_gate_bites_on_corruption(tmp_path: Path) -> None:
    """The failing fixture: a one-byte corruption of an exported event is caught
    (the importer refuses the tampered file — the gate cannot silently pass)."""
    engine = _fresh_engine()
    keychain = FakeKeychain()
    _seed_via_linear(engine, keychain)
    with Session(engine) as session:
        bundle = build_bundle(
            session,
            blob_store=LocalBlobStore(tmp_path / "src"),
            now=FIXED_NOW,
            device_key=device_private_key(keychain),
        )

    files = _members(bundle)
    name = "collections/tickets/events.ndjson"
    files[name] = (
        files[name].replace(b"Imported", b"CORRUPTED", 1)
        if b"Imported" in files[name]
        else files[name][:-2] + b"X\n"
    )
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    corrupted = raw.getvalue()

    # The corrupted bundle must NOT import cleanly (manifest hash / verify catches it).
    from kantaq_runtime.import_bundle import BundleImportError

    rt_bad = tmp_path / "rt-bad"
    rt_bad.mkdir()
    with pytest.raises(BundleImportError):
        check(corrupted, rt_bad)
