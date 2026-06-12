"""Attachment blob store: content addressing, untrusted-name handling (E12-T2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kantaq_core.tracker import (
    MAX_ATTACHMENT_BYTES,
    BlobError,
    BlobNotFoundError,
    BlobTooLargeError,
    LocalBlobStore,
    sanitize_filename,
)


@pytest.fixture
def store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def test_store_and_open_round_trip(store: LocalBlobStore) -> None:
    data = b"report bytes"
    ref = store.store(data, filename="report.pdf", media_type="application/pdf")
    assert ref.blob_id == hashlib.sha256(data).hexdigest()
    assert ref.size_bytes == len(data)
    assert store.open(ref.blob_id) == data


def test_same_content_stored_once(store: LocalBlobStore, tmp_path: Path) -> None:
    a = store.store(b"same", filename="a.txt", media_type="text/plain")
    b = store.store(b"same", filename="b.txt", media_type="text/plain")
    assert a.blob_id == b.blob_id
    stored_files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(stored_files) == 1


def test_oversize_attachment_is_refused(store: LocalBlobStore) -> None:
    with pytest.raises(BlobTooLargeError):
        store.store(b"x" * (MAX_ATTACHMENT_BYTES + 1), filename="big", media_type="x")


def test_missing_and_malformed_ids_fail_closed(store: LocalBlobStore) -> None:
    with pytest.raises(BlobNotFoundError):
        store.open("0" * 64)
    with pytest.raises(BlobNotFoundError):
        store.open("../../etc/passwd")  # not a hash: refused before any path math


def test_tampered_blob_fails_its_hash_check(store: LocalBlobStore, tmp_path: Path) -> None:
    ref = store.store(b"original", filename="f", media_type="x")
    path = tmp_path / "blobs" / ref.blob_id[:2] / ref.blob_id
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(BlobError, match="content-hash"):
        store.open(ref.blob_id)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
        ("  spaced name.txt  ", "spaced_name.txt"),
        ("évil‮xe.txt", "vil_xe.txt"),  # bidi-override and accents collapse
        ("", "attachment"),
        ("...", "attachment"),
    ],
)
def test_untrusted_filenames_are_sanitized(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_long_filenames_keep_their_suffix() -> None:
    name = sanitize_filename("a" * 300 + ".tar.gz")
    assert len(name) <= 120
    assert name.endswith(".gz")
