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
from typing import Any

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


class LocalBlobStore:
    """Content-addressed files under ``<root>/<aa>/<sha256>`` (0600)."""

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
