"""The portable export bundle producer (E23-T1, MOD-23, FR-E23-1..3).

A workspace exports to a deterministic gzip tarball any conformant client can
re-import — the data-sovereignty proof (PRD §17). The bundle is a bespoke
protocol format built on stdlib (`tarfile`/`gzip`/`json`/`hashlib`) plus reuse
of MOD-04 (`compose_snapshot`, the event log) and MOD-17 (`encode_canonical`,
`encode_canonical_grant`, `sign_bytes`); no third-party bundle standard is
claimed (honest-naming — we cite tar/gzip/JSON Lines/SHA-256/Ed25519).

Layout (MOD-23, locked):
  manifest.json                      the signed root of trust
  team_manifest.json                 {team_id, name, collections[]}
  actors.ndjson                      members + devices (the verification roots)
  grants.ndjson                      canonical, signed CapabilityGrants
  collections/<c>/snapshot.ndjson    the deterministic fold
  collections/<c>/events.ndjson      canonical signed events, resolution order
  blobs/manifest.json                {blob_id: BlobRef}
  blobs/data/<blob_id>               raw bytes (content-addressed)
  audit/anchors.json                 the hash-chain anchor over the local trail
  audit/policies.json                the audit policy descriptor

Self-verifying: `manifest.json` records the sha256 + entry count of every other
file and carries the device's signature over the canonical manifest-minus-
signature, so verifying that one signature then each file against its recorded
hash proves the whole bundle. Deterministic: same store + same ``now`` + same
``since`` → byte-identical tarball (sorted entries, fixed mtime/uid/gid/mode,
sorted-key compact JSON), which is the producer half of round-trip idempotence.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from kantaq_core import audit
from kantaq_core.identity import local_grant_index
from kantaq_core.tracker import LocalBlobStore
from kantaq_db.models import AuditEvent, Device, Member, Ticket, Workspace
from kantaq_protocol import canonicalize, encode_canonical, encode_canonical_grant, sign_bytes
from kantaq_sync_engine import SYNCABLE_MODELS, collection_rows, compose_snapshot, row_to_event

BUNDLE_FORMAT = "kantaq-bundle/v1"
# Domain separation: the manifest signature can never be replayed as an event
# or grant signature (mirrors MOD-17's per-type tags).
MANIFEST_SIGNING_DOMAIN = b"kantaq:export-manifest:v1\x00"

_AGENT_ROLE = "Agent"


def _line(obj: Any) -> str:
    """One deterministic JSON Lines record (sorted keys, compact)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def _ndjson(objs: list[Any]) -> bytes:
    return "".join(_line(obj) for obj in objs).encode("utf-8")


def _actors(session: Session) -> bytes:
    """One protocol ``Actor`` per line: members (human/agent) + devices (the
    Ed25519 verification roots), id-sorted."""
    rows: list[dict[str, Any]] = []
    for member in session.exec(select(Member)).all():
        kind = "agent" if member.role == _AGENT_ROLE else "human"
        rows.append(
            {"actor_id": member.id, "kind": kind, "public_key": None, "label": member.email}
        )
    for device in session.exec(select(Device)).all():
        rows.append(
            {
                "actor_id": device.id,
                "kind": "device",
                "public_key": device.public_key,
                "label": device.label,
            }
        )
    return _ndjson(sorted(rows, key=lambda row: row["actor_id"]))


def _team_manifest(session: Session, workspace: Workspace) -> bytes:
    """The workspace self-description: id, name, and the syncable-collection
    declarations (actors live in actors.ndjson — no duplication)."""
    manifest = {
        "team_id": workspace.id,
        "name": workspace.name,
        "collections": sorted(SYNCABLE_MODELS),
    }
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _grants(session: Session) -> bytes:
    """One canonical, signed ``CapabilityGrant`` per line, id-sorted."""
    grants, _revoked = local_grant_index(session)
    lines = [encode_canonical_grant(grants[gid]).decode("utf-8") for gid in sorted(grants)]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _collection_events(session: Session, collection: str, since: int | None) -> tuple[bytes, int]:
    """Canonical signed events for one collection in resolution order, filtered
    to ``committed_rev > since`` when incremental. Returns (bytes, high-water rev)."""
    high = 0
    lines: list[str] = []
    for row in collection_rows(session, collection):
        if row.committed_rev is not None:
            high = max(high, row.committed_rev)
        if since is not None and (row.committed_rev is None or row.committed_rev <= since):
            continue
        lines.append(encode_canonical(row_to_event(row)).decode("utf-8"))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"), high


def _blobs(session: Session, blob_store: LocalBlobStore) -> tuple[bytes, dict[str, bytes]]:
    """The attachments referenced by exported tickets: a BlobRef manifest plus
    the raw bytes (each verified against its content address on read)."""
    refs: dict[str, dict[str, Any]] = {}
    for ticket in session.exec(select(Ticket)).all():
        for attachment in ticket.attachments:
            blob_id = attachment["blob_id"]
            refs[blob_id] = {
                "blob_id": blob_id,
                "filename": attachment.get("filename", ""),
                "media_type": attachment.get("media_type", "application/octet-stream"),
                "size_bytes": attachment.get("size_bytes", 0),
                "sha256": blob_id,  # the address *is* the sha256 (MOD-03)
            }
    data = {blob_id: blob_store.open(blob_id) for blob_id in sorted(refs)}
    manifest = {blob_id: refs[blob_id] for blob_id in sorted(refs)}
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    ), data


def _audit(session: Session) -> tuple[bytes, bytes]:
    """The hash-chain anchor over the local audit trail + the policy descriptor."""
    first = session.exec(select(AuditEvent).order_by(AuditEvent.id).limit(1)).first()
    last = session.exec(
        select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)  # type: ignore[attr-defined]
    ).first()
    anchor: dict[str, Any] = (
        {
            "anchor_id": last.id,
            "range_start": first.id if first else last.id,
            "range_end": last.id,
            "chain_hash": last.chain_hash,
        }
        if last is not None
        else {}
    )
    policies = {
        "sources": list(audit.SOURCES),
        "append_only": True,
        "no_update": True,
        "no_delete": True,
    }
    anchor_bytes = (json.dumps(anchor, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    policy_bytes = (json.dumps(policies, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    return anchor_bytes, policy_bytes


def _targz(files: dict[str, bytes]) -> bytes:
    """A deterministic gzip tarball: sorted entries, zeroed mtime/uid/gid."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path in sorted(files):
            data = files[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()


def build_bundle(
    session: Session,
    *,
    blob_store: LocalBlobStore,
    now: datetime,
    device_key: str | None = None,
    since: int | None = None,
) -> bytes:
    """Produce the portable export tarball (FR-E23-1..3).

    ``device_key`` (the runtime device seed) signs the manifest when present;
    without it the manifest is integrity-checked by file hashes only. ``since``
    makes ``events.ndjson`` incremental (``committed_rev > since``); snapshots
    stay the full current fold either way.
    """
    workspace = session.exec(select(Workspace)).first()
    if workspace is None:
        raise ExportError("no workspace to export")

    files: dict[str, bytes] = {
        "team_manifest.json": _team_manifest(session, workspace),
        "actors.ndjson": _actors(session),
        "grants.ndjson": _grants(session),
    }

    cursor = 0
    for collection in sorted(SYNCABLE_MODELS):
        files[f"collections/{collection}/snapshot.ndjson"] = compose_snapshot(
            session, collection
        ).encode("utf-8")
        events, high = _collection_events(session, collection, since)
        files[f"collections/{collection}/events.ndjson"] = events
        cursor = max(cursor, high)

    blob_manifest, blob_data = _blobs(session, blob_store)
    files["blobs/manifest.json"] = blob_manifest
    for blob_id, payload in blob_data.items():
        files[f"blobs/data/{blob_id}"] = payload

    anchor_bytes, policy_bytes = _audit(session)
    files["audit/anchors.json"] = anchor_bytes
    files["audit/policies.json"] = policy_bytes

    files["manifest.json"] = _manifest(files, workspace.id, now, since, cursor, device_key)
    return _targz(files)


def _manifest(
    files: dict[str, bytes],
    workspace_id: str,
    now: datetime,
    since: int | None,
    cursor: int,
    device_key: str | None,
) -> bytes:
    """The signed root of trust: every other file's sha256 + count, then the
    device signature over the canonical manifest-minus-signature."""
    file_index = {
        path: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for path, data in sorted(files.items())
    }
    core: dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "workspace_id": workspace_id,
        "created_at": now.replace(microsecond=0).isoformat(),
        "since": since,
        "cursor": cursor,
        "files": file_index,
    }
    signature = (
        sign_bytes(MANIFEST_SIGNING_DOMAIN + canonicalize(core), device_key)
        if device_key is not None
        else None
    )
    manifest = {**core, "signature": signature}
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class ExportError(Exception):
    """The workspace cannot be exported (e.g. nothing to export)."""
