#!/usr/bin/env python
"""E23-T2 — the export round-trip checker (MOD-23, FR-E23-2).

Run it against a bundle exported from any workspace — including the real dogfood
workspace — to prove the round-trip is lossless: import the bundle into a fresh,
throwaway runtime, re-export it, and confirm byte-identical event logs, identical
snapshots, and content-addressed blobs. Exits non-zero on any drift.

This is the manual v0.1 procedure (the v0.2 CI gate automates exactly this on a
fixture workspace — see apps/local-runtime/tests/test_import_roundtrip.py). It
reads only the bundle you hand it and writes only to a temp dir it deletes, so
no workspace data is persisted or committed.

Usage:
    uv run python scripts/roundtrip_check.py path/to/kantaq-export.tar.gz

To produce the bundle from a running runtime:
    curl -s -X POST http://127.0.0.1:3939/v1/export \\
         -H "Authorization: Bearer $(uv run kantaq token show)" \\
         -o kantaq-export.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from kantaq_core.tracker import LocalBlobStore
from kantaq_runtime.export import build_bundle
from kantaq_runtime.import_bundle import import_bundle
from kantaq_sync_engine import SYNCABLE_MODELS


def _members(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        out: dict[str, bytes] = {}
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                if handle is not None:
                    out[member.name] = handle.read()
        return out


def check(bundle: bytes, workdir: Path) -> list[str]:
    """Import → re-export → compare. Returns a list of drift messages (empty == pass)."""
    engine = create_engine(f"sqlite:///{workdir / 'fresh.sqlite'}")
    SQLModel.metadata.create_all(engine)
    blobs = LocalBlobStore(workdir / "blobs")

    with Session(engine) as session:
        result = import_bundle(bundle, session=session, blob_store=blobs)
        session.commit()
    print(f"  imported {result.events} event(s), {result.blobs} blob(s)")

    with Session(engine) as session:
        reexport = build_bundle(session, blob_store=blobs, now=_created_at(bundle), device_key=None)

    src = _members(bundle)
    dst = _members(reexport)
    problems: list[str] = []

    for collection in sorted(SYNCABLE_MODELS):
        for kind in ("events", "snapshot"):
            name = f"collections/{collection}/{kind}.ndjson"
            if src.get(name) != dst.get(name):
                problems.append(f"{kind} drift in {collection}")

    src_blobs = json.loads(src.get("blobs/manifest.json", b"{}"))
    for blob_id, ref in src_blobs.items():
        if ref["sha256"] != blob_id:
            problems.append(f"blob {blob_id} manifest hash != its address")
        data = dst.get(f"blobs/data/{blob_id}")
        if data is None or hashlib.sha256(data).hexdigest() != blob_id:
            problems.append(f"blob {blob_id} did not survive content-addressed")
    return problems


def _created_at(bundle: bytes):
    """Re-export at the source's created_at so timestamps never cause drift."""
    from datetime import datetime

    manifest = json.loads(_members(bundle)["manifest.json"])
    return datetime.fromisoformat(manifest["created_at"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an export bundle round-trips losslessly.")
    parser.add_argument("bundle", type=Path, help="path to a kantaq export .tar.gz")
    args = parser.parse_args()

    bundle = args.bundle.read_bytes()
    print(f"checking {args.bundle} ({len(bundle)} bytes)…")
    with tempfile.TemporaryDirectory(prefix="kantaq-roundtrip-") as tmp:
        problems = check(bundle, Path(tmp))

    if problems:
        print("ROUND-TRIP FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("ROUND-TRIP OK: byte-identical event logs, identical snapshots, verified blob hashes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
