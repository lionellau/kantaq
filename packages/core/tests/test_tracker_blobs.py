"""Attachment blob store: content addressing, untrusted-name handling (E12-T2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kantaq_core.tracker import (
    MAX_ATTACHMENT_BYTES,
    BlobError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
    LocalBlobStore,
    S3BlobStore,
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


# --------------------------------------------------------- S3 object storage


class FakeS3Client:
    """An in-memory stand-in for the boto3 S3 client surface S3BlobStore uses.

    Mirrors boto3 closely enough to exercise the adapter without a network or
    the optional dependency: a missing key raises on get/head (boto3 raises
    ``ClientError``; the adapter treats any get/head failure as "absent")."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, str]:
        self.put_calls += 1
        self.objects[(Bucket, Key)] = Body
        return {"ETag": hashlib.md5(Body).hexdigest()}  # noqa: S324 - ETag shape only

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        data = self.objects[(Bucket, Key)]  # KeyError when absent, like a 404

        class _Body:
            def read(self) -> bytes:
                return data

        return {"Body": _Body()}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, int]:
        data = self.objects[(Bucket, Key)]  # KeyError when absent
        return {"ContentLength": len(data)}


@pytest.fixture
def s3_store() -> S3BlobStore:
    return S3BlobStore(FakeS3Client(), "kantaq-blobs", prefix="blobs/")


def test_s3_satisfies_the_blobstore_port(s3_store: S3BlobStore, store: LocalBlobStore) -> None:
    # Both implementations are structurally the same port (runtime_checkable).
    assert isinstance(s3_store, BlobStore)
    assert isinstance(store, BlobStore)


def test_s3_store_and_open_round_trip(s3_store: S3BlobStore) -> None:
    data = b"report bytes"
    ref = s3_store.store(data, filename="report.pdf", media_type="application/pdf")
    assert ref.blob_id == hashlib.sha256(data).hexdigest()
    assert ref.size_bytes == len(data)
    assert s3_store.open(ref.blob_id) == data
    assert s3_store.exists(ref.blob_id)


def test_s3_content_addressed_store_is_idempotent() -> None:
    client = FakeS3Client()
    s3 = S3BlobStore(client, "b")
    s3.store(b"same", filename="a.txt", media_type="text/plain")
    s3.store(b"same", filename="b.txt", media_type="text/plain")
    # The second store sees the object already present and skips the PUT.
    assert client.put_calls == 1
    assert len(client.objects) == 1


def test_s3_oversize_is_refused(s3_store: S3BlobStore) -> None:
    with pytest.raises(BlobTooLargeError):
        s3_store.store(b"x" * (MAX_ATTACHMENT_BYTES + 1), filename="big", media_type="x")


def test_s3_missing_and_malformed_ids_fail_closed(s3_store: S3BlobStore) -> None:
    assert not s3_store.exists("0" * 64)
    with pytest.raises(BlobNotFoundError):
        s3_store.open("0" * 64)
    with pytest.raises(BlobNotFoundError):
        s3_store.open("../../etc/passwd")  # not a hash: refused before any S3 call


def test_s3_tampered_object_fails_its_hash_check() -> None:
    client = FakeS3Client()
    s3 = S3BlobStore(client, "b")
    ref = s3.store(b"original", filename="f", media_type="x")
    # Corrupt the stored object behind the adapter's back.
    client.objects[("b", s3._key(ref.blob_id))] = b"tampered"
    with pytest.raises(BlobError, match="content-hash"):
        s3.open(ref.blob_id)


def test_s3_key_layout_is_content_addressed(s3_store: S3BlobStore) -> None:
    blob_id = "a" * 64
    assert s3_store._key(blob_id) == f"blobs/aa/{blob_id}"


def test_s3_from_config_without_boto3_explains_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_boto3(name: str, *args: object, **kwargs: object) -> object:
        if name == "boto3":
            raise ModuleNotFoundError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)
    with pytest.raises(BlobError, match="s3.*extra"):
        S3BlobStore.from_config(bucket="b")
