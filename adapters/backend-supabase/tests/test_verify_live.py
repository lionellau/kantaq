"""E24-T5 — verified ingestion against real Postgres + RLS, via FakePostgREST.

The deny suite's hermetic core is in `kantaq_sync_engine`'s test_verify_ingest;
this is the live half the sprint asks for (reuses the EphemeralPostgres seed +
the auth stub from Sprint 1). A member's runtime signs and pushes through the
real adapter (RLS permitting), then a ``VerifyingBackend`` over the same adapter
keeps the events that verify and drops the seed's **unsigned** legacy event —
exactly "the backend refuses what it cannot verify", enforced at the boundary
because Postgres cannot run Ed25519 until the v0.2 atomic RPC (D-09).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

from kantaq_backend_supabase.sync import SupabaseSyncBackend
from kantaq_protocol import CapabilityGrant, generate_keypair, sign, sign_grant
from kantaq_sync_engine import Event, VerifyContext, VerifyingBackend, verify_event
from kantaq_test_harness.postgrest import FakePostgREST, encode_test_jwt
from kantaq_test_harness.rls import supabase_claims

ANON_KEY = "anon-key-for-tests"
NOW = 1_767_225_600
BOB = "mbr_bob"
BOB_DEVICE = "dev_bob".ljust(26, "0")
BOB_GRANT = "grt_bob".ljust(26, "0")


def _adapter(engine: Engine, email: str, workspace_id: str) -> SupabaseSyncBackend:
    fake = FakePostgREST(engine)
    token = encode_test_jwt(supabase_claims(email))
    return SupabaseSyncBackend(
        fake.base_url,
        ANON_KEY,
        workspace_id=workspace_id,
        access_token=lambda: token,
        client=fake.client(),
    )


def _bob_world() -> tuple[str, CapabilityGrant, str]:
    """(device private key, signed grant, device public key) for member bob."""
    kp = generate_keypair()
    grant = sign_grant(
        CapabilityGrant(
            grant_id=BOB_GRANT,
            subject=BOB,
            issuer=BOB_DEVICE,
            resource="workspace/ws_a",
            verbs=("tickets.write",),
            issued_at=NOW - 60,
            expires_at=NOW + 3600,
        ),
        kp.private_key,
    )
    return kp.private_key, grant, kp.public_key


def _signed_event(private_key: str, seq: int, **payload: Any) -> Event:
    return sign(
        Event(
            event_id=f"evt_bob_{seq:017d}",
            collection="tickets",
            entity_id="tkt_bob",
            actor_id=BOB,
            actor_seq=seq,
            policy_ref=BOB_GRANT,
            payload=payload or {"title": f"v{seq}"},
        ),
        private_key,
    )


def test_verifying_pull_keeps_signed_and_drops_unsigned_seed(sync_pg: Engine) -> None:
    private_key, grant, public_key = _bob_world()
    adapter = _adapter(sync_pg, "bob@acme.dev", "ws_a")

    # Bob signs and pushes through the real adapter — RLS lets him write as himself.
    adapter.push([_signed_event(private_key, 1), _signed_event(private_key, 2)])

    denied: list[tuple[str, str]] = []
    context = VerifyContext(
        roots={BOB_DEVICE: public_key},
        grants={BOB_GRANT: grant},
        now=NOW,
    )
    verifying = VerifyingBackend(
        adapter,
        context=lambda: context,
        on_deny=lambda event, verdict: denied.append((event.actor_id, verdict.code)),
    )

    kept = verifying.pull(collection="tickets")
    entities = {entry.event.entity_id for entry in kept}

    # Bob's signed events verified and folded; the seeded unsigned event did not.
    assert "tkt_bob" in entities
    assert "tkt_a" not in entities  # the Sprint-1 seed has no signature
    assert ("mbr_alice", "unsigned") in denied
    # And the kept stream re-verifies clean (no silently-passed bytes).
    assert all(verify_event(entry.event, context).ok for entry in kept)
