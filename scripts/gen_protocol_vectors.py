"""Regenerate the MOD-17 protocol golden vectors (D-11, FR-E03-6).

Writes ``packages/test_harness/fixtures/protocol_golden_vectors.json``:
deterministic events and grants, their canonical signing bytes, and Ed25519
signatures — generated with the chosen library (pyca/cryptography) and
cross-verified here, at generation time, with PyNaCl (libsodium), so the
checked-in file was never self-referential even before the test suite runs.

The private keys below are **published test fixtures** (fixed, documented,
never used outside tests) — the same convention RFC 8032 §7.1 uses.

Run from the repo root:  uv run python scripts/gen_protocol_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

import nacl.signing

from kantaq_protocol import (
    CapabilityGrant,
    Event,
    grant_signing_bytes,
    public_key_of,
    sign,
    sign_grant,
    signing_bytes,
)

# Fixed test seeds (NOT secrets): byte patterns chosen to be obviously synthetic.
DEVICE_A_SEED = bytes(range(32)).hex()  # 000102...1f
DEVICE_B_SEED = (b"\xab" * 32).hex()

EVENTS: list[tuple[str, Event, str]] = [
    (
        "minimal_patch",
        Event(
            event_id="01JTESTEVENT00000000000001",
            collection="tickets",
            entity_id="01JTESTTICKET0000000000001",
            actor_id="01JTESTDEVICEA000000000001",
            actor_seq=1,
            payload={"status": "doing"},
        ),
        DEVICE_A_SEED,
    ),
    (
        "full_envelope",
        Event(
            event_id="01JTESTEVENT00000000000002",
            collection="agent_proposals",
            entity_id="01JTESTPROPOSAL00000000001",
            actor_id="01JTESTDEVICEA000000000001",
            actor_seq=2,
            op="patch",
            base_rev=41,
            policy_ref="grant/01JTESTGRANT00000000000001",
            payload={"status": "approved", "labels": ["bug", "p1"], "estimate": 3},
        ),
        DEVICE_A_SEED,
    ),
    (
        "unicode_key_order",
        # Keys exercise the RFC 8785 UTF-16 code-unit sort: "０" (BMP,
        # high) sorts AFTER "\U0001d306" (supplementary plane) under UTF-16
        # code units because the surrogate pair starts at 0xD834 < 0xFF10 —
        # the case a naive code-point sort gets wrong.
        Event(
            event_id="01JTESTEVENT00000000000003",
            collection="comments",
            entity_id="01JTESTCOMMENT000000000001",
            actor_id="01JTESTDEVICEB000000000001",
            actor_seq=1,
            op="append",
            payload={"\U0001d306": "supplementary", "０": "bmp", "é": "latin"},
        ),
        DEVICE_B_SEED,
    ),
    (
        "tombstone_empty_payload",
        Event(
            event_id="01JTESTEVENT00000000000004",
            collection="tickets",
            entity_id="01JTESTTICKET0000000000002",
            actor_id="01JTESTDEVICEB000000000001",
            actor_seq=2,
            op="tombstone",
            payload={},
        ),
        DEVICE_B_SEED,
    ),
]

GRANTS: list[tuple[str, CapabilityGrant, str]] = [
    (
        "member_grant",
        CapabilityGrant(
            grant_id="01JTESTGRANT00000000000001",
            subject="01JTESTMEMBER0000000000001",
            issuer="01JTESTDEVICEA000000000001",
            resource="workspace/01JTESTWS00000000000000001",
            verbs=("tickets.read", "tickets.write"),
            issued_at=1_767_225_600,  # 2026-01-01T00:00:00Z
            expires_at=1_767_312_000,  # +24h
        ),
        DEVICE_A_SEED,
    ),
    (
        "agent_grant_short_lived",
        CapabilityGrant(
            grant_id="01JTESTGRANT00000000000002",
            subject="01JTESTAGENT00000000000001",
            issuer="01JTESTDEVICEB000000000001",
            resource="workspace/01JTESTWS00000000000000001",
            verbs=("tickets.read", "proposals.write"),
            issued_at=1_767_225_600,
            expires_at=1_767_229_200,  # +1h (the agent default)
            revokes="01JTESTGRANT00000000000001",
        ),
        DEVICE_B_SEED,
    ),
]


def _cross_verify(message: bytes, sig_hex: str, public_key_hex: str) -> None:
    """PyNaCl (libsodium) must accept what cryptography (OpenSSL) signed."""
    verify_key = nacl.signing.VerifyKey(bytes.fromhex(public_key_hex))
    verify_key.verify(message, bytes.fromhex(sig_hex))  # raises BadSignatureError


def main() -> None:
    vectors: dict[str, list[dict[str, object]]] = {"events": [], "grants": []}

    for name, event, seed in EVENTS:
        signed = sign(event, seed)
        message = signing_bytes(event)
        assert signed.sig is not None
        _cross_verify(message, signed.sig, public_key_of(seed))
        vectors["events"].append(
            {
                "name": name,
                "event": {
                    "event_id": event.event_id,
                    "collection": event.collection,
                    "entity_id": event.entity_id,
                    "actor_id": event.actor_id,
                    "actor_seq": event.actor_seq,
                    "op": event.op,
                    "base_rev": event.base_rev,
                    "policy_ref": event.policy_ref,
                    "payload": event.payload,
                },
                "private_key_hex": seed,
                "public_key_hex": public_key_of(seed),
                "signing_bytes_hex": message.hex(),
                "sig_hex": signed.sig,
            }
        )

    for name, grant, seed in GRANTS:
        signed_grant = sign_grant(grant, seed)
        message = grant_signing_bytes(grant)
        assert signed_grant.sig is not None
        _cross_verify(message, signed_grant.sig, public_key_of(seed))
        vectors["grants"].append(
            {
                "name": name,
                "grant": {
                    "grant_id": grant.grant_id,
                    "subject": grant.subject,
                    "issuer": grant.issuer,
                    "resource": grant.resource,
                    "verbs": list(grant.verbs),
                    "issued_at": grant.issued_at,
                    "expires_at": grant.expires_at,
                    "revokes": grant.revokes,
                },
                "private_key_hex": seed,
                "public_key_hex": public_key_of(seed),
                "signing_bytes_hex": message.hex(),
                "sig_hex": signed_grant.sig,
            }
        )

    out = Path(__file__).resolve().parents[1] / (
        "packages/test_harness/fixtures/protocol_golden_vectors.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(vectors, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(vectors['events'])} events, {len(vectors['grants'])} grants)")


if __name__ == "__main__":
    main()
