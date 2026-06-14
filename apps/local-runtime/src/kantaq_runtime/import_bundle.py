"""Import the inverse of :mod:`kantaq_runtime.export` (E23-T2, MOD-23).

This is the importer half of the portability proof: read a bundle produced by
``build_bundle`` and reconstruct its event log + content-addressed blobs into a
fresh runtime, so an export → import → re-export round-trip yields byte-identical
event logs, identical snapshots, and matching blob hashes (FR-E23-2).

Scope (v0.1): this is a *library* function — the round-trip gate and the
documented manual procedure call it directly. The public ``POST /v1/import``
endpoint with its own auth surface stays v0.1-out, v0.2-in (DEBT-03); this
function is the seam it will call.

Verification on ingest mirrors the backend (E24-T5): every *signed* event is
checked against its issuing device's root key (found via its capability grant);
a tamper is refused. Unsigned events are accepted as immutable pre-cutover
history (D-15). The manifest is integrity-checked against its recorded per-file
hashes, and its signature (when present) is verified against a device root —
verifying the signed root then each file proves the whole bundle.

Identity distribution (actors/grants/audit) travels in the manifest and is not
folded into a domain table here; the round-trip's named guarantee is event logs,
snapshots, and blob hashes (the rest is v0.2 full-bundle import).
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from kantaq_core.tracker import LocalBlobStore
from kantaq_db.models import EventLog, Workspace
from kantaq_protocol import canonicalize, decode, decode_grant, verify, verify_bytes
from kantaq_runtime.export import MANIFEST_SIGNING_DOMAIN
from kantaq_sync_engine import insert_event
from kantaq_sync_engine.apply import refold_entity

# Fold parents before children so a refold never violates a foreign key.
_REFOLD_ORDER = (
    "workspaces",
    "members",
    "projects",
    "tickets",
    "comments",
    "ticket_relationships",
    "agent_proposals",
    "memory_entries",
    "memory_links",
)


class BundleImportError(Exception):
    """The bundle is malformed, fails its integrity check, or carries an event
    that does not verify (fail closed — nothing is imported)."""


@dataclass(frozen=True)
class ImportResult:
    workspace_id: str
    events: int
    blobs: int


def _untar(bundle: bytes) -> dict[str, bytes]:
    raw = gzip.decompress(bundle)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                files[member.name] = handle.read()
    return files


def _ndjson_lines(raw: bytes) -> list[str]:
    return [line for line in raw.decode("utf-8").splitlines() if line.strip()]


def _device_roots(actors_ndjson: bytes) -> dict[str, str]:
    """{device_id: public_key} — the Ed25519 verification roots (actors.ndjson)."""
    roots: dict[str, str] = {}
    for line in _ndjson_lines(actors_ndjson):
        actor = json.loads(line)
        if actor.get("kind") == "device" and actor.get("public_key"):
            roots[actor["actor_id"]] = actor["public_key"]
    return roots


def _grant_issuers(grants_ndjson: bytes) -> dict[str, str]:
    """{grant_id: issuer_device_id} from the canonical, signed grants."""
    issuers: dict[str, str] = {}
    for line in _ndjson_lines(grants_ndjson):
        grant = decode_grant(line.encode("utf-8"))
        issuers[grant.grant_id] = grant.issuer
    return issuers


def _verify_file_hashes(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
    import hashlib

    for path, entry in manifest.get("files", {}).items():
        if path not in files:
            raise BundleImportError(f"bundle is missing a manifest-listed file: {path}")
        actual = hashlib.sha256(files[path]).hexdigest()
        if actual != entry["sha256"]:
            raise BundleImportError(f"file {path} failed its manifest hash check")


def _verify_manifest_signature(manifest: dict[str, Any], roots: dict[str, str]) -> None:
    signature = manifest.get("signature")
    if signature is None:
        return  # hashes-only integrity (no device key signed this bundle)
    core = {key: value for key, value in manifest.items() if key != "signature"}
    message = MANIFEST_SIGNING_DOMAIN + canonicalize(core)
    if not any(verify_bytes(message, signature, key) for key in roots.values()):
        raise BundleImportError("manifest signature does not verify against any device root")


def import_bundle(bundle: bytes, *, session: Session, blob_store: LocalBlobStore) -> ImportResult:
    """Reconstruct ``bundle`` into the fresh runtime behind ``session`` and
    ``blob_store``. The caller owns the transaction commit."""
    files = _untar(bundle)
    for required in ("manifest.json", "team_manifest.json", "actors.ndjson"):
        if required not in files:
            raise BundleImportError(f"bundle is missing {required}")

    manifest = json.loads(files["manifest.json"])
    _verify_file_hashes(files, manifest)
    roots = _device_roots(files["actors.ndjson"])
    _verify_manifest_signature(manifest, roots)
    issuers = _grant_issuers(files.get("grants.ndjson", b""))

    team = json.loads(files["team_manifest.json"])
    workspace_id = team["team_id"]
    if session.get(Workspace, workspace_id) is None:
        session.add(Workspace(id=workspace_id, name=team["name"]))
        session.flush()

    # Insert events in bundle (resolution) order, assigning committed_rev so the
    # imported log re-folds — and re-exports — in exactly the same order.
    rev = 0
    for collection in team["collections"]:
        for line in _ndjson_lines(files.get(f"collections/{collection}/events.ndjson", b"")):
            event = decode(line.encode("utf-8"))
            if event.sig is not None:
                device = issuers.get(event.policy_ref or "")
                root = roots.get(device or "")
                if root is None or not verify(event, root):
                    raise BundleImportError(
                        f"event {event.event_id} did not verify against a known device root"
                    )
            rev += 1
            insert_event(session, event, committed_rev=rev)
    session.flush()

    # Fold the imported log into the domain tables so the runtime is usable and
    # re-exports identically (parents first, so a child's FK always resolves).
    present = set(team["collections"])
    ordered = [c for c in _REFOLD_ORDER if c in present]
    ordered += [c for c in present if c not in _REFOLD_ORDER]
    for collection in ordered:
        entity_ids = session.exec(
            select(EventLog.entity_id).where(EventLog.collection == collection).distinct()
        ).all()
        for entity_id in entity_ids:
            refold_entity(session, collection, entity_id)

    blob_count = 0
    blob_manifest = json.loads(files.get("blobs/manifest.json", b"{}"))
    for blob_id, ref in blob_manifest.items():
        data = files.get(f"blobs/data/{blob_id}")
        if data is None:
            raise BundleImportError(f"blob {blob_id} is listed but its data is missing")
        stored = blob_store.store(
            data,
            filename=ref.get("filename", ""),
            media_type=ref.get("media_type", "application/octet-stream"),
        )
        if stored.blob_id != blob_id:
            raise BundleImportError(f"blob {blob_id} failed its content-address check on import")
        blob_count += 1

    return ImportResult(workspace_id=workspace_id, events=rev, blobs=blob_count)
