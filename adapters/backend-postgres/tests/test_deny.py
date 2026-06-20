"""E25-T1 [SEC]: the self-hosted commit re-runs the full deny matrix.

The self-hosted backend builds its ``VerifyContext`` from its OWN trust tables
(``verification_roots`` / ``local_grant_index``) and runs the SHARED
``verify_event`` — so every grant check the Supabase RPC enforces is enforced
here, by the same function, against real Postgres. This suite mints real
Ed25519 keypairs, seeds a member + device + signed grant, and asserts that:

- a correctly signed event under a live grant commits (the positive control);
- each failure mode (unsigned past cutover, tampered bytes, missing grant,
  revoked device, revoked grant, expired window, wrong workspace scope, wrong
  verb) is denied with the right structured code AND commits **nothing**; and
- a batch where any event fails is rejected atomically — the good events in it
  do not commit either.

Because the self-hosted server is Python, ``verify_event`` here also verifies
the Ed25519 *bytes* — the one check the plpgsql RPC cannot do (D-09). So the
self-hosted backend is a strict superset of the Supabase server-side posture
(``test_tampered_byte`` would pass through the RPC's presence-only check but is
caught here).
"""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_backend_postgres import PostgresSyncBackend
from kantaq_backend_postgres.schema import sync_events
from kantaq_db.models import CapabilityGrantRow, Device, Member
from kantaq_protocol import CapabilityGrant, generate_keypair, sign, sign_grant
from kantaq_sync_engine import INVALID_SIGNATURE, POLICY_DENIED, UNSIGNED, Event, EventRejected

from .conftest import WORKSPACE_ID

NOW = 1_767_225_600  # a fixed instant inside the grant window
MEMBER = "mbr_alice".ljust(26, "0")
DEVICE = "dev_alice".ljust(26, "0")
GRANT = "grt_self0".ljust(26, "0")
_seq = itertools.count(1)
_eid = itertools.count(1)


def _seed(
    engine: Engine,
    public_key: str,
    *,
    resource: str = WORKSPACE_ID,
    verbs: tuple[str, ...] = ("tickets.write",),
    issued_at: int = NOW - 60,
    expires_at: int = NOW + 3600,
    device_revoked: bool = False,
    grant_revoked: bool = False,
) -> str:
    """Seed a member + device + signed grant into the server's trust tables.

    Returns the grant's stored ``sig`` is not needed; the grant id is fixed
    (``GRANT``). The grant is signed with the device's private key so
    ``verify_grant`` accepts it (or, with ``device_revoked``/``grant_revoked``,
    so the deny path is the *state*, not a bad signature)."""
    grant_proto = CapabilityGrant(
        grant_id=GRANT,
        subject=MEMBER,
        issuer=DEVICE,
        resource=resource,
        verbs=verbs,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    signed = sign_grant(grant_proto, _signing_key)
    from datetime import UTC, datetime

    ts = datetime.fromtimestamp(NOW, UTC).replace(tzinfo=None)
    revoked_ts = ts if grant_revoked else None
    with Session(engine) as session:
        # Flush in FK dependency order (members → devices → capability_grants);
        # the models declare FK columns but no ORM relationship, so the
        # unit-of-work does not topologically sort these for us.
        session.add(
            Member(id=MEMBER, workspace_id=WORKSPACE_ID, email="alice@acme.dev", role="Owner")
        )
        session.flush()
        session.add(
            Device(
                id=DEVICE,
                public_key=public_key,
                member_id=MEMBER,
                label="alice laptop",
                revoked_at=ts if device_revoked else None,
            )
        )
        session.flush()
        session.add(
            CapabilityGrantRow(
                id=GRANT,
                subject=MEMBER,
                issuer=DEVICE,
                resource=resource,
                verbs=list(verbs),
                issued_at=issued_at,
                expires_at=expires_at,
                sig=signed.sig,
                revoked_at=revoked_ts,
            )
        )
        session.commit()
    return signed.sig


def _event(*, sign_it: bool = True, **overrides: object) -> Event:
    base = Event(
        event_id=f"e{next(_eid):025d}",
        collection="tickets",
        entity_id="tkt_deny0".ljust(26, "0"),
        actor_id=MEMBER,
        actor_seq=next(_seq),
        policy_ref=GRANT,
        payload={"title": "v1"},
    )
    base = replace(base, **overrides)
    return sign(base, _signing_key) if sign_it else base


def _row_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(sync_events)).scalar_one())


# One keypair for the whole module; the seed signs the grant with it and events
# are signed with it, so the positive control verifies end to end.
_kp = generate_keypair()
_signing_key = _kp.private_key


def _backend(engine: Engine, *, now: int = NOW) -> PostgresSyncBackend:
    return PostgresSyncBackend(engine, workspace_id=WORKSPACE_ID, now=lambda: now)


# ------------------------------------------------------------- positive control


def test_signed_event_under_live_grant_commits(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key)
    out = _backend(pg_engine).commit_events([_event()], require_signature=True)
    assert out[0].status == "committed"
    assert _row_count(pg_engine) == 1


# ----------------------------------------------------------------- the deny matrix


def test_unsigned_past_cutover_is_rejected_and_commits_nothing(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key)
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([_event(sign_it=False)], require_signature=True)
    assert exc.value.code == UNSIGNED
    assert _row_count(pg_engine) == 0


def test_tampered_byte_breaks_the_signature(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key)
    signed = _event()
    tampered = replace(signed, payload={"title": "TAMPERED"})  # sig now stale
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([tampered], require_signature=True)
    assert exc.value.code == INVALID_SIGNATURE
    assert _row_count(pg_engine) == 0


def test_missing_grant_is_policy_denied(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key)
    event = _event(policy_ref="grt_unknown".ljust(26, "0"))
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([event], require_signature=True)
    assert exc.value.code == POLICY_DENIED
    assert _row_count(pg_engine) == 0


def test_revoked_device_is_no_longer_a_root(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key, device_revoked=True)
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([_event()], require_signature=True)
    assert exc.value.code == POLICY_DENIED
    assert _row_count(pg_engine) == 0


def test_revoked_grant_is_policy_denied(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key, grant_revoked=True)
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([_event()], require_signature=True)
    assert exc.value.code == POLICY_DENIED
    assert _row_count(pg_engine) == 0


def test_expired_grant_is_policy_denied(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key, expires_at=NOW + 10)
    # a backend whose clock is past the grant's expiry
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine, now=NOW + 100).commit_events([_event()], require_signature=True)
    assert exc.value.code == POLICY_DENIED
    assert _row_count(pg_engine) == 0


def test_grant_for_another_workspace_is_denied(pg_engine: Engine) -> None:
    _seed(pg_engine, _kp.public_key, resource="ws_other00000000000000000")
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([_event()], require_signature=True)
    assert exc.value.code == POLICY_DENIED
    assert _row_count(pg_engine) == 0


def test_grant_without_the_collection_verb_is_denied(pg_engine: Engine) -> None:
    # a memory-only grant cannot authorise a ticket write (D-03 per-verb scoping)
    _seed(pg_engine, _kp.public_key, verbs=("memory.write",))
    with pytest.raises(EventRejected) as exc:
        _backend(pg_engine).commit_events([_event()], require_signature=True)
    assert exc.value.code == POLICY_DENIED
    assert _row_count(pg_engine) == 0


def test_a_batch_with_one_bad_event_commits_nothing(pg_engine: Engine) -> None:
    """Atomic reject (pass 1): a good event riding with a bad one does not land."""
    _seed(pg_engine, _kp.public_key)
    good = _event()
    bad = _event(sign_it=False)  # unsigned past cutover
    with pytest.raises(EventRejected):
        _backend(pg_engine).commit_events([good, bad], require_signature=True)
    assert _row_count(pg_engine) == 0
