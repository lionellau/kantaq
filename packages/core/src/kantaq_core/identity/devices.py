"""Device identity: the runtime's Ed25519 keypair (E06-T4, FR-E06-3, D-01).

Each local runtime is the ``device`` actor: at boot it ensures one Ed25519
keypair exists — the private seed parked in the runtime keychain (the 0600
``FileKeychain``; the OS-keychain golden-rule re-pass came back the same as
v0.0.5 — no library clears the reuse bar, see docs/stack.md), the verify key
registered as a ``devices`` row. The browser never sees key material
(RISK-03), and **the private key never leaves this module's keychain reads**:
it is not on the row, not in any API response, not in any log line, and not
in any sync payload (sprint exit criterion 3, test-pinned).

The set of active device rows is the root-of-trust map (`roots`) grant
verification resolves issuers against. Device rows sync like any collection
(teammates need each other's roots), emitted through the standard event-log
seam; backend-side verification of what they sign is Sprint 4 (E24-T5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, col, select

from kantaq_core import audit
from kantaq_core.identity.keychain import Keychain
from kantaq_core.tracker.events import DomainEvent, EventSink
from kantaq_db.models import Device
from kantaq_protocol import generate_keypair, public_key_of

# The keychain entry holding this runtime's Ed25519 private seed (hex).
DEVICE_KEY_NAME = "device-key"


def ensure_device(
    session: Session,
    keychain: Keychain,
    *,
    member_id: str | None = None,
    label: str = "local runtime",
    sink: EventSink | None = None,
    now: datetime | None = None,
) -> Device:
    """Boot-time idempotent device identity (FR-E06-3).

    First boot generates a keypair, parks the seed in the keychain, registers
    the verify key as a ``devices`` row (audited, and emitted to the event
    log so the registration reaches the backend through normal sync). Every
    later boot finds the seed and returns the existing row. If the keychain
    holds a seed but the row is missing (a wiped replica), the row is
    re-registered from the seed's public key — the keychain is the identity,
    the row is its registration.
    """
    ts = now or datetime.now(UTC).replace(tzinfo=None)
    seed = keychain.get(DEVICE_KEY_NAME)
    if seed is None:
        keys = generate_keypair()
        keychain.set(DEVICE_KEY_NAME, keys.private_key)
        public_key = keys.public_key
    else:
        public_key = public_key_of(seed)

    existing = session.exec(select(Device).where(Device.public_key == public_key)).first()
    if existing is not None:
        return existing

    device = Device(
        public_key=public_key,
        member_id=member_id,
        label=label,
        created_at=ts,
        updated_at=ts,
    )
    session.add(device)
    session.flush()
    audit.write(
        session,
        actor_id=member_id or device.id,
        action="device.register",
        source="app",
        object_ref=f"devices/{device.id}",
        after=audit.snapshot(device),
        now=ts,
    )
    if sink is not None:
        sink.emit(
            DomainEvent(
                collection="devices",
                entity_id=device.id,
                op="patch",
                payload=audit.snapshot(device),
            )
        )
    return device


def device_private_key(keychain: Keychain) -> str | None:
    """The runtime's signing seed — keychain-only, for the grant issuer."""
    return keychain.get(DEVICE_KEY_NAME)


def local_device(session: Session, keychain: Keychain) -> Device | None:
    """The device row matching this runtime's keychain seed, if registered."""
    seed = keychain.get(DEVICE_KEY_NAME)
    if seed is None:
        return None
    public_key = public_key_of(seed)
    return session.exec(select(Device).where(Device.public_key == public_key)).first()


def verification_roots(session: Session) -> dict[str, str]:
    """Active device id -> verify key: the offline-verification root map."""
    rows = session.exec(select(Device).where(col(Device.revoked_at).is_(None))).all()
    return {row.id: row.public_key for row in rows}
