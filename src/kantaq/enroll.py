"""Encrypted one-shot onboarding bundles (``kantaq enroll``).

Design: ``docs/design/enroll.md``. The bundle (``.kqe``) is a versioned,
self-describing JSON envelope: an argon2id-derived key (passcode → key;
salt + cost parameters ride the header) sealing an XSalsa20-Poly1305
``SecretBox`` ciphertext that carries the joiner's backend coordinates and
member token. Everything semantic lives *inside* the ciphertext, so any
tamper — header or body — fails authenticated decryption closed.

This module is a transport lockbox for a credential, not a protocol object:
it signs nothing and verifies nothing, which is why it lives in the umbrella
package beside the CLI rather than in ``packages/protocol``. The identity
work is the existing spine, reused verbatim: ``seed_member`` /
``IdentityService.rotate_token`` (provision), ``SyncServerBackend.whoami`` +
``IdentityService.adopt_owner`` (the DEBT-42 join), ``ensure_device_identity``
(device keypair + verification root), ``IdentityService.revoke_member``
(cascade revoke). Nothing here mints, hashes, or checks a credential by hand.

Secrets discipline: the plaintext ``kq_…`` token appears only in the minting
transaction's return value, sealed inside the ciphertext, and (after import)
in the joiner's keychain-backed runtime. It is never logged and never printed.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nacl.exceptions
import nacl.pwhash
import nacl.secret
import nacl.utils

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.engine import Engine

ENVELOPE_FORMAT = "kantaq-enroll/v1"
PAYLOAD_FORMAT = "kantaq-enroll-payload/v1"
KDF_NAME = "argon2id"
# Domain-separated KDF input (protocol.md "Domain separation"): this derivation
# can never collide with any other passcode use.
PASSCODE_DOMAIN = b"kantaq:enroll-passcode:v1\x00"

DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days
SALT_BYTES = nacl.pwhash.argon2id.SALTBYTES  # 16
NONCE_BYTES = nacl.secret.SecretBox.NONCE_SIZE  # 24
KEY_BYTES = nacl.secret.SecretBox.KEY_SIZE  # 32

# Bounded inputs (the canonical codec's adversarial-hardening rule, applied to
# this envelope): the file is tiny by construction, and a hostile header must
# not be able to turn import into a memory/CPU bomb — the caps are checked
# BEFORE the KDF runs.
#
# The accepted ceiling is EXACTLY the profile we seal with (MODERATE: t=3,
# 256 MiB), NOT SENSITIVE (t=4, 1 GiB). Accepting up to SENSITIVE bought nothing
# — ``seal`` only ever emits MODERATE or the test MIN — and let a hostile file
# make ``import`` run a heavier Argon2id than any real bundle ever would (~1 GiB
# / ~20 s × the attempt loop; adversarial-review H1). Capping at MODERATE means
# the worst an attacker's header can cost is exactly what opening a legitimate
# bundle costs. To retune the sealing cost later, raise ``_kdf_profile`` AND
# these caps together (a deliberate, tested one-liner — never a silent bomb).
MAX_BUNDLE_BYTES = 64 * 1024
MAX_KDF_OPSLIMIT = nacl.pwhash.argon2id.OPSLIMIT_MODERATE
MAX_KDF_MEMLIMIT = nacl.pwhash.argon2id.MEMLIMIT_MODERATE

# Test-only KDF profile, selected by the SAME switch as token hashing
# (DEBT-18, set only by the root conftest.py): the suite must not be dominated
# by a deliberate-cost KDF. Import reads the parameters out of the header, so a
# bundle sealed under any profile opens under any other; production never sets
# the flag, and the moderate profile is never weakened at runtime.
_ARGON2_TEST_FAST_ENV = "KANTAQ_ARGON2_TEST_FAST"

# Passcode alphabet: lowercase minus i/l/o, digits minus 0/1 — 31 unambiguous
# characters. Six groups of four ≈ 119 bits, far past exhaustion even before
# the memory-hard KDF.
_PASSCODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_PASSCODE_GROUPS = 6
_PASSCODE_GROUP_LEN = 4

BACKEND_MODE_POSTGRES = "postgres"
IMPORT_MAX_PASSCODE_ATTEMPTS = 3


class EnrollError(Exception):
    """Base class for enrollment errors (the CLI prints these verbatim)."""


class BundleFormatError(EnrollError):
    """The bundle is not a well-formed ``kantaq-enroll/v1`` envelope."""


class BundleDecryptError(EnrollError):
    """Authenticated decryption failed: wrong passcode or a tampered bundle.

    Deliberately one error for both — the envelope offers no oracle that would
    let an attacker distinguish a near-miss from a tamper.
    """

    def __init__(self) -> None:
        super().__init__("decryption failed: wrong passcode or tampered bundle")


class BundleExpiredError(EnrollError):
    """The bundle's TTL has elapsed; the owner must export a fresh one."""


@dataclass(frozen=True)
class EnrollPayload:
    """What the ciphertext carries. ``hub_token`` is the credential."""

    backend_mode: str
    hub_url: str
    hub_token: str
    member_id: str
    member_email: str
    workspace_id: str
    workspace_name: str
    issued_at: int
    expires_at: int


_PAYLOAD_STR_FIELDS = (
    "backend_mode",
    "hub_url",
    "hub_token",
    "member_id",
    "member_email",
    "workspace_id",
    "workspace_name",
)
_PAYLOAD_INT_FIELDS = ("issued_at", "expires_at")
_ENVELOPE_KEYS = frozenset({"format", "kdf", "salt", "opslimit", "memlimit", "nonce", "ciphertext"})


def generate_passcode() -> str:
    """A fresh ~119-bit passcode, grouped for transcription over a call."""
    groups = (
        "".join(secrets.choice(_PASSCODE_ALPHABET) for _ in range(_PASSCODE_GROUP_LEN))
        for _ in range(_PASSCODE_GROUPS)
    )
    return "-".join(groups)


def _kdf_profile() -> tuple[int, int]:
    """``(opslimit, memlimit)`` for sealing — moderate, or the test-only MIN."""
    if os.environ.get(_ARGON2_TEST_FAST_ENV) == "1":
        return nacl.pwhash.argon2id.OPSLIMIT_MIN, nacl.pwhash.argon2id.MEMLIMIT_MIN
    return (
        nacl.pwhash.argon2id.OPSLIMIT_MODERATE,
        nacl.pwhash.argon2id.MEMLIMIT_MODERATE,
    )


def _derive_key(passcode: str, salt: bytes, opslimit: int, memlimit: int) -> bytes:
    return nacl.pwhash.argon2id.kdf(
        KEY_BYTES,
        PASSCODE_DOMAIN + passcode.encode("utf-8"),
        salt,
        opslimit=opslimit,
        memlimit=memlimit,
    )


def seal(payload: EnrollPayload, passcode: str) -> bytes:
    """Seal a payload into ``.kqe`` envelope bytes under a passcode."""
    opslimit, memlimit = _kdf_profile()
    salt = nacl.utils.random(SALT_BYTES)
    key = _derive_key(passcode, salt, opslimit, memlimit)
    box = nacl.secret.SecretBox(key)
    nonce = nacl.utils.random(NONCE_BYTES)
    plaintext = json.dumps({"format": PAYLOAD_FORMAT, **asdict(payload)}, sort_keys=True).encode(
        "utf-8"
    )
    sealed = box.encrypt(plaintext, nonce)
    envelope = {
        "format": ENVELOPE_FORMAT,
        "kdf": KDF_NAME,
        "salt": salt.hex(),
        "opslimit": opslimit,
        "memlimit": memlimit,
        "nonce": nonce.hex(),
        "ciphertext": base64.b64encode(bytes(sealed.ciphertext)).decode("ascii"),
    }
    return (json.dumps(envelope, sort_keys=True) + "\n").encode("utf-8")


def _hex_field(header: dict[str, Any], key: str, expected_len: int) -> bytes:
    value = header[key]
    if not isinstance(value, str):
        raise BundleFormatError(f"{key} must be a hex string")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise BundleFormatError(f"{key} is not valid hex") from exc
    if len(raw) != expected_len or value != raw.hex():
        raise BundleFormatError(f"{key} must be {expected_len} lowercase hex bytes")
    return raw


def _parse_envelope(data: bytes) -> tuple[bytes, int, int, bytes, bytes]:
    """Strict envelope parse → ``(salt, opslimit, memlimit, nonce, ciphertext)``.

    Refuses oversized files, unknown keys, unknown formats, bad types, and
    KDF parameters past the SENSITIVE cap — all before the KDF runs.
    """
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleFormatError(f"bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    try:
        header = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleFormatError("bundle is not a JSON envelope") from exc
    if not isinstance(header, dict):
        raise BundleFormatError("bundle envelope must be a JSON object")
    if set(header) != _ENVELOPE_KEYS:
        raise BundleFormatError("bundle envelope has missing or unknown keys")
    if header["format"] != ENVELOPE_FORMAT:
        raise BundleFormatError(f"unknown bundle format: {header['format']!r}")
    if header["kdf"] != KDF_NAME:
        raise BundleFormatError(f"unknown kdf: {header['kdf']!r}")
    opslimit, memlimit = header["opslimit"], header["memlimit"]
    if (
        not isinstance(opslimit, int)
        or not isinstance(memlimit, int)
        or isinstance(opslimit, bool)
        or isinstance(memlimit, bool)
    ):
        raise BundleFormatError("kdf parameters must be integers")
    if not (nacl.pwhash.argon2id.OPSLIMIT_MIN <= opslimit <= MAX_KDF_OPSLIMIT):
        raise BundleFormatError("opslimit outside the accepted range")
    if not (nacl.pwhash.argon2id.MEMLIMIT_MIN <= memlimit <= MAX_KDF_MEMLIMIT):
        raise BundleFormatError("memlimit outside the accepted range")
    if memlimit % 1024:
        # libsodium rounds memlimit to 1 KiB granularity, so two memlimit
        # spellings could derive one key — one statement has one spelling.
        raise BundleFormatError("memlimit must be a multiple of 1024")
    salt = _hex_field(header, "salt", SALT_BYTES)
    nonce = _hex_field(header, "nonce", NONCE_BYTES)
    ciphertext_b64 = header["ciphertext"]
    if not isinstance(ciphertext_b64, str):
        raise BundleFormatError("ciphertext must be a base64 string")
    try:
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BundleFormatError("ciphertext is not valid base64") from exc
    if base64.b64encode(ciphertext).decode("ascii") != ciphertext_b64:
        # b64decode ignores the final group's trailing bits, so a flipped bit
        # there would decode to the same bytes — refuse non-canonical spellings,
        # exactly like the codec's re-encode check.
        raise BundleFormatError("ciphertext is not canonical base64")
    return salt, opslimit, memlimit, nonce, ciphertext


def _parse_payload(plaintext: bytes) -> EnrollPayload:
    """Strict payload parse: exact key set, exact types, known formats only."""
    try:
        body = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleFormatError("payload is not JSON") from exc
    if not isinstance(body, dict):
        raise BundleFormatError("payload must be a JSON object")
    expected = {"format", *_PAYLOAD_STR_FIELDS, *_PAYLOAD_INT_FIELDS}
    if set(body) != expected:
        raise BundleFormatError("payload has missing or unknown keys")
    if body["format"] != PAYLOAD_FORMAT:
        raise BundleFormatError(f"unknown payload format: {body['format']!r}")
    for key in _PAYLOAD_STR_FIELDS:
        if not isinstance(body[key], str) or not body[key]:
            raise BundleFormatError(f"payload field {key} must be a non-empty string")
    for key in _PAYLOAD_INT_FIELDS:
        if not isinstance(body[key], int) or isinstance(body[key], bool):
            raise BundleFormatError(f"payload field {key} must be an integer")
    # SSRF hardening (adversarial-review M2): ``import`` GETs ``hub_url/v1/me``
    # before any identity check can matter, so a malicious sealer could aim the
    # joiner at a ``file://`` or link-local metadata URL. Enforce the http(s)
    # scheme HERE, at unseal — the CLI's export-side check does not protect the
    # importer. (The export CLI validates its own ``--hub-url`` too, defence in
    # depth.)
    if not body["hub_url"].startswith(("http://", "https://")):
        raise BundleFormatError("hub_url must be an http(s) URL")
    if body["expires_at"] <= body["issued_at"]:
        raise BundleFormatError("payload validity is inverted (expires_at <= issued_at)")
    if body["backend_mode"] != BACKEND_MODE_POSTGRES:
        # Fail closed rather than half-join: the Supabase mode is designed but
        # deferred (docs/design/enroll.md §6, DEBT-43).
        raise BundleFormatError(
            f"backend_mode {body['backend_mode']!r} is not supported yet (docs/design/enroll.md §6)"
        )
    return EnrollPayload(**{k: body[k] for k in (*_PAYLOAD_STR_FIELDS, *_PAYLOAD_INT_FIELDS)})


def unseal(data: bytes, passcode: str) -> EnrollPayload:
    """Open envelope bytes under a passcode; every failure is fail-closed.

    Raises :class:`BundleFormatError` for anything structurally wrong and
    :class:`BundleDecryptError` for a wrong passcode or any tampered byte —
    the Poly1305 tag authenticates the ciphertext, and every header field
    participates in key derivation or decryption, so there is no byte an
    attacker can flip that still opens.
    """
    salt, opslimit, memlimit, nonce, ciphertext = _parse_envelope(data)
    key = _derive_key(passcode, salt, opslimit, memlimit)
    try:
        plaintext = nacl.secret.SecretBox(key).decrypt(ciphertext, nonce)
    except nacl.exceptions.CryptoError as exc:
        raise BundleDecryptError() from exc
    return _parse_payload(plaintext)


def ensure_not_expired(payload: EnrollPayload, *, now: int | None = None) -> None:
    """TTL check — runs before any state changes on import."""
    ts = int(datetime.now(UTC).timestamp()) if now is None else now
    if ts >= payload.expires_at:
        expired = datetime.fromtimestamp(payload.expires_at, UTC).isoformat()
        raise BundleExpiredError(
            f"bundle expired at {expired}; ask the owner to run "
            "`kantaq enroll export` again (re-export also rotates the credential)"
        )


def parse_ttl(value: str) -> int:
    """``30d`` / ``12h`` / ``45m`` / ``900s`` / plain seconds → seconds."""
    text = value.strip().lower()
    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    multiplier = 1
    if text and text[-1] in unit_seconds:
        multiplier = unit_seconds[text[-1]]
        text = text[:-1]
    try:
        amount = int(text)
    except ValueError as exc:
        raise EnrollError(f"cannot parse ttl {value!r} (try 30d, 12h, 45m, or seconds)") from exc
    if amount <= 0:
        raise EnrollError("ttl must be positive")
    return amount * multiplier


# --------------------------------------------------------------------------
# Owner side: provision (backend host, same posture as `seed`)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionedMember:
    member_id: str
    workspace_id: str
    workspace_name: str
    token_plaintext: str
    rotated: bool  # True when re-export revoked + re-minted an existing member's token


def provision_enrollment(
    engine: Engine, *, email: str, workspace: str, role: str = "Member"
) -> ProvisionedMember:
    """Provision (or re-provision) the joiner on the backend database.

    New email → ``seed_member`` (workspace + member + token, exactly the path
    ``python -m kantaq_backend_postgres.seed`` runs today). Existing email →
    ``IdentityService.rotate_token``: the previous enrollment's token is
    revoked in the same transaction that mints the new one, so re-export is
    idempotent per (email, workspace) and tokens never accumulate. A revoked
    member refuses — reviving one is a deliberate admin action, not an enroll
    side effect (and its token would be dead on arrival at ``TokenVerifier``).

    ``workspace`` must match the backend's workspace (by name or id) when one
    exists — the guard against provisioning into the wrong backend.
    """
    from sqlmodel import Session, select

    from kantaq_backend_postgres.seed import seed_member
    from kantaq_core.identity import IdentityService
    from kantaq_db.models import Member, Workspace

    with Session(engine) as session:
        existing_workspace = session.exec(select(Workspace)).first()
        if existing_workspace is not None and workspace not in (
            existing_workspace.id,
            existing_workspace.name,
        ):
            raise EnrollError(
                f"this backend's workspace is {existing_workspace.name!r} "
                f"({existing_workspace.id}); refusing to enroll into {workspace!r} — "
                "wrong --workspace or wrong --database-url?"
            )
        member = session.exec(select(Member).where(Member.email == email)).first()
        if member is not None:
            if member.status == "revoked":
                raise EnrollError(
                    f"member {email} is revoked; enrollment will not revive a revoked "
                    "member (see docs/design/enroll.md §7)"
                )
            minted = IdentityService(session).rotate_token(member.id)
            ws = session.get(Workspace, member.workspace_id)
            assert ws is not None  # FK-guaranteed
            return ProvisionedMember(
                member_id=member.id,
                workspace_id=ws.id,
                workspace_name=ws.name,
                token_plaintext=minted.plaintext,
                rotated=True,
            )

    member_id, token_plaintext = seed_member(
        engine, email=email, workspace_name=workspace, role=role
    )
    with Session(engine) as session:
        seeded = session.get(Member, member_id)
        assert seeded is not None  # seed_member just committed it
        ws = session.get(Workspace, seeded.workspace_id)
        assert ws is not None
        return ProvisionedMember(
            member_id=member_id,
            workspace_id=ws.id,
            workspace_name=ws.name,
            token_plaintext=token_plaintext,
            rotated=False,
        )


def revoke_enrollment(engine: Engine, *, email: str) -> tuple[str, bool]:
    """Revoke an enrolled member by email: tokens + grants + devices, audited.

    Thin lookup over ``IdentityService.revoke_member`` — the cascade (and the
    last-Owner guard) is entirely the existing path. Propagation is < 5 s by
    construction: the sync-server authenticates every request through
    ``TokenVerifier``, whose cache TTL is pinned under the NFR-E06-2 budget.
    Returns ``(member_id, already_revoked)``.
    """
    from sqlmodel import Session, select

    from kantaq_core.identity import IdentityService
    from kantaq_db.models import Member

    with Session(engine) as session:
        member = session.exec(select(Member).where(Member.email == email)).first()
        if member is None:
            raise EnrollError(f"no member with email {email}")
        if member.status == "revoked":
            return member.id, True  # idempotent — already revoked
        IdentityService(session).revoke_member(member.id)
        return member.id, False


def engine_for(database_url: str) -> Engine:
    """A backend engine from a URL — exactly what ``seed``'s CLI does."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url)


# --------------------------------------------------------------------------
# Joiner side: import (fresh runtime → ready for `kantaq sync once`)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    member_id: str
    member_email: str
    workspace_name: str
    already_joined: bool
    env_path: Path
    env_backup: Path | None


def write_private(path: Path, data: bytes) -> None:
    """Write a secret-bearing file with 0600 from its very first byte.

    ``write_bytes`` + ``chmod`` would leave a umask-permissions window between
    creation and the chmod; opening with the mode closes it. An existing file
    is re-chmodded so a previously looser file tightens rather than persists.

    ``O_NOFOLLOW`` refuses to write a secret *through* a symlink (adversarial-
    review M3): an attacker who can pre-plant ``.env`` (or the backup path) as a
    symlink to a file they can read must not capture the token. A symlinked
    target raises a clear :class:`EnrollError` rather than silently writing
    through it.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EnrollError(
            f"refusing to write {path}: not a regular file (a symlink?) — {exc.strerror}"
        ) from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    path.chmod(0o600)


def _write_env(env_path: Path, *, hub_url: str, hub_token: str) -> tuple[Path, Path | None]:
    """Merge ``HUB_MODE``/``HUB_URL``/``HUB_TOKEN`` into ``.env`` (0600).

    Existing unrelated lines are preserved; an existing file is backed up
    beside itself first. The token's only unsealed resting place is this file
    plus the keychain — both are 0600 from creation.
    """
    updates = {
        "HUB_MODE": BACKEND_MODE_POSTGRES,
        "HUB_URL": hub_url,
        "HUB_TOKEN": hub_token,
    }
    backup: Path | None = None
    lines: list[str] = []
    if env_path.exists():
        original = env_path.read_text(encoding="utf-8")
        # Collision-safe backup name: the timestamp is second-granular, so two
        # imports in the same second would otherwise clobber the first backup
        # (adversarial-review M3). Append a counter until the name is free.
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        backup = env_path.with_name(f"{env_path.name}.bak-{stamp}")
        counter = 1
        while backup.exists():
            counter += 1
            backup = env_path.with_name(f"{env_path.name}.bak-{stamp}-{counter}")
        write_private(backup, original.encode("utf-8"))
        for line in original.splitlines():
            key = line.split("=", 1)[0].strip()
            if key in updates:
                continue  # replaced below
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in updates.items())
    write_private(env_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return env_path, backup


def import_enrollment(
    payload: EnrollPayload,
    *,
    local_db_path: str,
    keychain_dir: Path,
    env_path: Path,
    client: httpx.Client | None = None,
    now: int | None = None,
) -> ImportResult:
    """Join a backend from a decrypted payload — the DEBT-42 path, one-shot.

    Order matters (fail closed): TTL → server preflight (``/v1/me``: the
    credential is live AND is the member the bundle claims) → ensure the local
    schema (a fresh replica migrates via the normal ``kantaq db migrate`` path;
    a non-empty replica on a stale schema refuses rather than silently
    upgrading) → adopt identity → park the runtime token → ensure the device
    keypair + verification root → write ``.env``. The network + identity checks
    run before **any credential, identity, or config is written** — a
    forged/expired/mismatched bundle touches no disk. The schema-ensure that
    follows them may create an empty replica file (SQLite opens it on connect);
    that is not secret state, and every step after the checks is idempotent, so
    a partial failure (e.g. an ``adopt_owner`` refusal) is fixed by re-running.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlmodel import Session

    from kantaq_backend_postgres import SyncServerBackend
    from kantaq_core.identity import FileKeychain, IdentityService
    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_runtime.auth import RUNTIME_TOKEN_KEY, ensure_device_identity

    ensure_not_expired(payload, now=now)

    backend = SyncServerBackend(payload.hub_url, payload.hub_token, client=client)
    me = backend.whoami()  # SyncBackendError here → nothing written
    if me["member_id"] != payload.member_id or me["workspace_id"] != payload.workspace_id:
        raise EnrollError(
            "bundle/server identity mismatch: the server says this credential is "
            f"member {me['member_id']} in workspace {me['workspace_id']}, but the "
            f"bundle claims {payload.member_id} in {payload.workspace_id} — refusing"
        )

    engine = get_engine(sqlite_url(local_db_path))
    if not sa_inspect(engine).get_table_names():
        from kantaq_db import migrations

        migrations.upgrade(sqlite_url(local_db_path))
    else:
        from kantaq_db import schema_version

        check = schema_version.verify(engine)
        if not check.ok:
            raise EnrollError(
                f"local replica schema: {check.message} — run `kantaq db migrate`, "
                "then re-run the import"
            )
    keychain = FileKeychain(keychain_dir)
    with Session(engine) as session:
        minted = IdentityService(session).adopt_owner(
            member_id=me["member_id"],
            workspace_id=me["workspace_id"],
            email=me["email"] or payload.member_email,
            workspace_name=me["workspace_name"] or payload.workspace_name,
        )
    if minted is not None:
        keychain.set(RUNTIME_TOKEN_KEY, minted.plaintext)
    ensure_device_identity(engine, keychain)
    written, backup = _write_env(env_path, hub_url=payload.hub_url, hub_token=payload.hub_token)
    return ImportResult(
        member_id=me["member_id"],
        member_email=me["email"] or payload.member_email,
        workspace_name=me["workspace_name"] or payload.workspace_name,
        already_joined=minted is None,
        env_path=written,
        env_backup=backup,
    )
