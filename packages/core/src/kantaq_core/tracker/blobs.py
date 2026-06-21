"""Local blob store for ticket attachments (E12-T2, FR-E12-4, D-13).

Solo mode stores attachment bytes on the local filesystem next to the database
(D-13; team mode moves the bytes to Supabase Storage with E24's blob endpoint).
The store is content-addressed: ``blob_id`` is the SHA-256 of the bytes, so the
same file attached twice is stored once and a ref can always be re-verified
against its content (the hash-verify half of FR-E04-5).

Attachments are **untrusted files** (PRD §15): this module stores and returns
bytes, nothing else — no preview, no type sniffing, no execution. The declared
filename is sanitized to a bare basename before it is kept, and callers (the
runtime API) must serve downloads as opaque ``application/octet-stream``
attachments. No AV scanning in MVP (DEBT-10).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

# One attachment must fit comfortably in a sync event flow later; 10 MiB is
# generous for tracker use and small enough to never surprise the local disk.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

_FILENAME_MAX = 120
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class BlobError(Exception):
    """A blob could not be stored or read."""


class BlobTooLargeError(BlobError):
    def __init__(self, size: int) -> None:
        super().__init__(f"attachment is {size} bytes; the limit is {MAX_ATTACHMENT_BYTES}")
        self.size = size


class BlobNotFoundError(BlobError):
    def __init__(self, blob_id: str) -> None:
        super().__init__(f"no such blob: {blob_id}")
        self.blob_id = blob_id


def sanitize_filename(raw: str) -> str:
    """Reduce an untrusted filename to a safe bare basename.

    Path separators, traversal, control characters, and non-ASCII tricks are
    all collapsed; an empty result becomes ``attachment``. The original name is
    display metadata only — bytes are addressed by hash, never by name.
    """
    name = unicodedata.normalize("NFKC", raw)
    # Take the last path component under either separator convention.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _FILENAME_SAFE.sub("_", name).strip("._")
    if len(name) > _FILENAME_MAX:
        stem, _, suffix = name.rpartition(".")
        keep = _FILENAME_MAX - len(suffix) - 1 if suffix else _FILENAME_MAX
        name = (stem[:keep] + "." + suffix) if suffix else name[:_FILENAME_MAX]
    return name or "attachment"


@dataclass(frozen=True)
class AttachmentRef:
    """The JSON shape stored on ``tickets.attachments`` (never the bytes)."""

    blob_id: str  # sha256 hex of the content
    filename: str  # sanitized display name
    media_type: str  # as declared by the uploader; informational only
    size_bytes: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class BlobStore(Protocol):
    """The object-storage port (E25-T3 / FR-E25-3) — content-addressed bytes.

    Two implementations ship: :class:`LocalBlobStore` (filesystem, the
    single-host default per D-32) and :class:`S3BlobStore` (any S3-compatible
    object store — AWS S3, MinIO, Cloudflare R2 — the scalable option a
    self-hosting team points a shared bucket at so attachments are visible
    team-wide). Both are content-addressed by SHA-256 and re-verify bytes
    against their address on read, so a ref is always re-checkable and the same
    file stored twice is stored once. The export bundle (MOD-23) reads/writes
    through this port too, so a backup round-trips blobs regardless of backend.
    """

    def store(self, data: bytes, *, filename: str, media_type: str) -> AttachmentRef: ...

    def open(self, blob_id: str) -> bytes: ...

    def exists(self, blob_id: str) -> bool: ...


class LocalBlobStore:
    """Content-addressed files under ``<root>/<aa>/<sha256>`` (0600).

    The :class:`BlobStore` filesystem implementation (D-32 default). In
    self-host mode the bytes live on the replica's disk; a shared
    :class:`S3BlobStore` is the team-wide option, and the export bundle is the
    cross-replica backstop either way.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def store(self, data: bytes, *, filename: str, media_type: str) -> AttachmentRef:
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise BlobTooLargeError(len(data))
        blob_id = hashlib.sha256(data).hexdigest()
        path = self._path(blob_id)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.chmod(0o600)
            tmp.replace(path)
        return AttachmentRef(
            blob_id=blob_id,
            filename=sanitize_filename(filename),
            media_type=media_type or "application/octet-stream",
            size_bytes=len(data),
        )

    def open(self, blob_id: str) -> bytes:
        """Return the raw bytes, verifying them against their address."""
        path = self._path(blob_id)
        if not path.is_file():
            raise BlobNotFoundError(blob_id)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != blob_id:
            raise BlobError(f"blob {blob_id} failed its content-hash check")
        return data

    def exists(self, blob_id: str) -> bool:
        return self._path(blob_id).is_file()

    def _path(self, blob_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", blob_id):
            raise BlobNotFoundError(blob_id)
        return self._root / blob_id[:2] / blob_id


class _S3Client(Protocol):
    """The slice of the S3 client surface :class:`S3BlobStore` uses.

    boto3's S3 client, the MinIO client, and any compatible SDK satisfy this;
    tests pass an in-memory fake. Keeping the dependency duck-typed means
    ``kantaq_core`` carries no hard ``boto3`` import — the SDK is an optional
    extra resolved only in :meth:`S3BlobStore.from_config`.
    """

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> Any: ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...


class S3BlobStore:
    """Content-addressed blobs in an S3-compatible bucket (E25-T3 / D-32).

    The scalable :class:`BlobStore` option a self-hosting team points at a
    shared bucket (AWS S3 / MinIO / R2) so attachments are visible across
    replicas without an export round-trip. Same content-addressed contract as
    :class:`LocalBlobStore`: the key is ``<prefix><aa>/<sha256>``, a re-store of
    the same bytes is a no-op, and a read re-verifies the bytes against their
    address. The client is injected (see :class:`_S3Client`) so the adapter is
    testable with a fake and free of a hard ``boto3`` dependency.
    """

    def __init__(self, client: _S3Client, bucket: str, *, prefix: str = "blobs/") -> None:
        self._client = client
        self._bucket = bucket
        # Normalize to a single trailing slash (or empty) so keys are stable.
        self._prefix = (prefix.rstrip("/") + "/") if prefix else ""

    @classmethod
    def from_config(
        cls,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        prefix: str = "blobs/",
    ) -> S3BlobStore:
        """Build a store backed by a real boto3 S3 client (the ``s3`` extra).

        ``boto3`` is imported here, not at module load, so the dependency stays
        optional: installs without the extra never import it, and tests inject a
        fake client through ``__init__`` instead. Credentials fall through to
        boto3's default provider chain when not passed explicitly.
        """
        try:
            # Optional dependency (the `s3` extra), imported on demand; no stub
            # in the base env so mypy can't resolve it when boto3 isn't installed.
            import boto3  # type: ignore[import-not-found]  # noqa: PLC0415
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised via the message
            raise BlobError(
                "S3 blob storage needs the 's3' extra: install kantaq with `[s3]` "
                "(adds boto3), or set KANTAQ_BLOB_STORE=filesystem."
            ) from exc
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )
        return cls(client, bucket, prefix=prefix)

    def store(self, data: bytes, *, filename: str, media_type: str) -> AttachmentRef:
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise BlobTooLargeError(len(data))
        blob_id = hashlib.sha256(data).hexdigest()
        media_type = media_type or "application/octet-stream"
        # Content-addressed: an identical object is already stored, so skip the
        # PUT (idempotent, same as LocalBlobStore's exists() guard).
        if not self.exists(blob_id):
            self._client.put_object(
                Bucket=self._bucket, Key=self._key(blob_id), Body=data, ContentType=media_type
            )
        return AttachmentRef(
            blob_id=blob_id,
            filename=sanitize_filename(filename),
            media_type=media_type,
            size_bytes=len(data),
        )

    def open(self, blob_id: str) -> bytes:
        key = self._key(blob_id)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data: bytes = response["Body"].read()
        except Exception as exc:  # noqa: BLE001 — any read miss is "not found" for an opaque store
            raise BlobNotFoundError(blob_id) from exc
        if hashlib.sha256(data).hexdigest() != blob_id:
            raise BlobError(f"blob {blob_id} failed its content-hash check")
        return data

    def exists(self, blob_id: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(blob_id))
        except Exception:  # noqa: BLE001 — head raises (404/ClientError) when the key is absent
            return False
        return True

    def _key(self, blob_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", blob_id):
            raise BlobNotFoundError(blob_id)
        return f"{self._prefix}{blob_id[:2]}/{blob_id}"
