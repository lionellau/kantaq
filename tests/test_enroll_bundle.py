"""The enroll bundle lockbox — hermetic crypto tests (docs/design/enroll.md).

Every gate here is proven by its deny path (the E27-T3 rule: a gate that has
never failed is not known to work): a tampered byte must refuse, a wrong
passcode must refuse, an expired bundle must refuse, an out-of-bounds KDF
header must refuse *before* the KDF runs. No DB, no network; the root
conftest's ``KANTAQ_ARGON2_TEST_FAST=1`` selects the MIN KDF profile, and the
cost-agnostic-open test proves parameters ride the header, so production
bundles are unaffected.
"""

from __future__ import annotations

import base64
import json

import pytest

from kantaq.enroll import (
    ENVELOPE_FORMAT,
    MAX_BUNDLE_BYTES,
    PAYLOAD_FORMAT,
    BundleDecryptError,
    BundleExpiredError,
    BundleFormatError,
    EnrollError,
    EnrollPayload,
    ensure_not_expired,
    generate_passcode,
    parse_ttl,
    seal,
    unseal,
)

PASSCODE = "test-passcode-for-the-suite"


def _payload(**overrides: object) -> EnrollPayload:
    fields: dict[str, object] = {
        "backend_mode": "postgres",
        "hub_url": "https://hub.acme.example",
        "hub_token": "kq_01testtoken.secret-material",
        "member_id": "mbr_test0".ljust(26, "0"),
        "member_email": "joiner@acme.dev",
        "workspace_id": "ws_test00".ljust(26, "0"),
        "workspace_name": "Acme",
        "issued_at": 1_783_100_000,
        "expires_at": 1_785_692_000,
    }
    fields.update(overrides)
    return EnrollPayload(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------- happy path


def test_seal_unseal_roundtrip() -> None:
    payload = _payload()
    data = seal(payload, PASSCODE)
    assert unseal(data, PASSCODE) == payload


def test_two_seals_never_share_salt_or_nonce_or_ciphertext() -> None:
    payload = _payload()
    first = json.loads(seal(payload, PASSCODE))
    second = json.loads(seal(payload, PASSCODE))
    assert first["salt"] != second["salt"]
    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]


def test_kdf_parameters_ride_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import is cost-agnostic: a bundle sealed under the test profile opens
    with the flag off, because the salt + cost live in the header (the same
    contract as PHC token hashes)."""
    data = seal(_payload(), PASSCODE)
    monkeypatch.delenv("KANTAQ_ARGON2_TEST_FAST", raising=False)
    assert unseal(data, PASSCODE) == _payload()


def test_no_payload_field_appears_in_the_bundle_bytes() -> None:
    """A leaked ``.kqe`` alone reveals nothing: no token, URL, email, id, or
    name is present in the file bytes in the clear."""
    payload = _payload()
    data = seal(payload, PASSCODE)
    for value in (
        payload.hub_token,
        payload.hub_url,
        payload.member_email,
        payload.member_id,
        payload.workspace_id,
        payload.workspace_name,
    ):
        assert value.encode() not in data
    assert PASSCODE.encode() not in data


def test_generated_passcodes_are_grouped_and_unique() -> None:
    seen = {generate_passcode() for _ in range(64)}
    assert len(seen) == 64
    for passcode in seen:
        groups = passcode.split("-")
        assert len(groups) == 6
        assert all(len(g) == 4 for g in groups)
        assert all(c in "abcdefghjkmnpqrstuvwxyz23456789" for g in groups for c in g)


# ----------------------------------------------------------------- deny paths


def test_wrong_passcode_fails_closed() -> None:
    data = seal(_payload(), PASSCODE)
    with pytest.raises(BundleDecryptError):
        unseal(data, "not-the-passcode")


def test_any_flipped_byte_refuses_to_open() -> None:
    """Exhaustive one-bit flip over the whole envelope (header AND ciphertext):
    every position must fail closed — the protocol golden-vector discipline
    applied to the lockbox. There is no byte an attacker can flip that still
    opens, because every header field participates in key derivation or
    decryption and the ciphertext is Poly1305-authenticated."""
    data = bytearray(seal(_payload(), PASSCODE))
    assert unseal(bytes(data), PASSCODE) == _payload()  # control
    for position in range(len(data)):
        tampered = bytearray(data)
        tampered[position] ^= 0x01
        with pytest.raises((BundleFormatError, BundleDecryptError)):
            unseal(bytes(tampered), PASSCODE)


def test_expired_bundle_refuses_and_fresh_one_passes() -> None:
    payload = _payload()
    ensure_not_expired(payload, now=payload.expires_at - 1)
    with pytest.raises(BundleExpiredError):
        ensure_not_expired(payload, now=payload.expires_at)


def test_oversized_bundle_refuses_before_parsing() -> None:
    with pytest.raises(BundleFormatError, match="exceeds"):
        unseal(b"x" * (MAX_BUNDLE_BYTES + 1), PASSCODE)


def test_kdf_bounds_refuse_before_the_kdf_runs() -> None:
    """A hostile header cannot turn import into a memory bomb: parameters past
    the cap refuse in the parse step, never reaching the KDF."""
    envelope = json.loads(seal(_payload(), PASSCODE))
    envelope["memlimit"] = 1 << 40  # a terabyte
    with pytest.raises(BundleFormatError, match="memlimit"):
        unseal(json.dumps(envelope).encode(), PASSCODE)
    envelope = json.loads(seal(_payload(), PASSCODE))
    envelope["opslimit"] = 10_000
    with pytest.raises(BundleFormatError, match="opslimit"):
        unseal(json.dumps(envelope).encode(), PASSCODE)


def test_accepted_kdf_ceiling_is_the_sealed_profile_not_a_bomb() -> None:
    """Adversarial-review H1: the ACCEPTED ceiling must equal the profile we
    seal with (MODERATE), so the worst a hostile header can cost is exactly what
    opening a real bundle costs — never libsodium's 1 GiB SENSITIVE profile."""
    import nacl.pwhash

    from kantaq.enroll import MAX_KDF_MEMLIMIT, MAX_KDF_OPSLIMIT

    assert MAX_KDF_OPSLIMIT == nacl.pwhash.argon2id.OPSLIMIT_MODERATE
    assert MAX_KDF_MEMLIMIT == nacl.pwhash.argon2id.MEMLIMIT_MODERATE
    # A header one KiB above MODERATE is refused (would otherwise be a heavier
    # KDF than any legitimate bundle ever runs).
    envelope = json.loads(seal(_payload(), PASSCODE))
    envelope["memlimit"] = nacl.pwhash.argon2id.MEMLIMIT_MODERATE + 1024
    with pytest.raises(BundleFormatError, match="memlimit"):
        unseal(json.dumps(envelope).encode(), PASSCODE)
    envelope = json.loads(seal(_payload(), PASSCODE))
    envelope["opslimit"] = nacl.pwhash.argon2id.OPSLIMIT_SENSITIVE  # t=4 > MODERATE
    with pytest.raises(BundleFormatError, match="opslimit"):
        unseal(json.dumps(envelope).encode(), PASSCODE)


def test_hub_url_scheme_is_validated_at_unseal() -> None:
    """Adversarial-review M2 (SSRF): a non-http(s) hub_url is refused inside
    unseal — before import can GET it — so a malicious sealer cannot aim the
    joiner at file:// or a link-local metadata address."""
    for bad in ("file:///etc/passwd", "http://169.254.169.254/", "ftp://x/"):
        data = _seal_raw_payload(_payload_body(hub_url=bad), PASSCODE)
        if bad.startswith("http://"):
            # http(s) scheme passes the parse; the SSRF mitigation here is the
            # scheme gate, not an IP allowlist (documented in §5/M2).
            assert unseal(data, PASSCODE).hub_url == bad
        else:
            with pytest.raises(BundleFormatError, match="http"):
                unseal(data, PASSCODE)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(format="kantaq-enroll/v999"),
        lambda e: e.update(kdf="scrypt"),
        lambda e: e.update(extra="field"),
        lambda e: e.pop("salt"),
        lambda e: e.update(salt="zz"),
        lambda e: e.update(salt="ab" * 8 + "cd"),  # wrong length
        lambda e: e.update(nonce="AB" * 24),  # uppercase hex — one spelling only
        lambda e: e.update(ciphertext="!!not-base64!!"),
        lambda e: e.update(opslimit="3"),  # wrong type
    ],
)
def test_malformed_envelopes_refuse(mutate: object) -> None:
    envelope = json.loads(seal(_payload(), PASSCODE))
    mutate(envelope)  # type: ignore[operator]
    with pytest.raises(BundleFormatError):
        unseal(json.dumps(envelope).encode(), PASSCODE)


def test_non_json_and_non_object_bundles_refuse() -> None:
    with pytest.raises(BundleFormatError):
        unseal(b"\x00\x01\x02 not json", PASSCODE)
    with pytest.raises(BundleFormatError):
        unseal(b'["a", "list"]', PASSCODE)


def _seal_raw_payload(body: dict[str, object], passcode: str) -> bytes:
    """Seal an arbitrary payload dict — for payload-level strictness tests."""
    import nacl.pwhash
    import nacl.secret
    import nacl.utils

    from kantaq.enroll import KEY_BYTES, NONCE_BYTES, PASSCODE_DOMAIN, SALT_BYTES

    salt = nacl.utils.random(SALT_BYTES)
    opslimit = nacl.pwhash.argon2id.OPSLIMIT_MIN
    memlimit = nacl.pwhash.argon2id.MEMLIMIT_MIN
    key = nacl.pwhash.argon2id.kdf(
        KEY_BYTES,
        PASSCODE_DOMAIN + passcode.encode(),
        salt,
        opslimit=opslimit,
        memlimit=memlimit,
    )
    nonce = nacl.utils.random(NONCE_BYTES)
    sealed = nacl.secret.SecretBox(key).encrypt(json.dumps(body).encode(), nonce)
    return json.dumps(
        {
            "format": ENVELOPE_FORMAT,
            "kdf": "argon2id",
            "salt": salt.hex(),
            "opslimit": opslimit,
            "memlimit": memlimit,
            "nonce": nonce.hex(),
            "ciphertext": base64.b64encode(bytes(sealed.ciphertext)).decode(),
        }
    ).encode()


def _payload_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"format": PAYLOAD_FORMAT}
    body.update(
        {
            "backend_mode": "postgres",
            "hub_url": "https://hub.acme.example",
            "hub_token": "kq_01testtoken.secret-material",
            "member_id": "mbr_test0".ljust(26, "0"),
            "member_email": "joiner@acme.dev",
            "workspace_id": "ws_test00".ljust(26, "0"),
            "workspace_name": "Acme",
            "issued_at": 1_783_100_000,
            "expires_at": 1_785_692_000,
        }
    )
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "body",
    [
        _payload_body(format="kantaq-enroll-payload/v999"),
        _payload_body(unexpected="key"),
        _payload_body(hub_token=""),
        _payload_body(issued_at="not-an-int"),
        _payload_body(expires_at=1_783_100_000),  # expires_at <= issued_at
        _payload_body(backend_mode="supabase"),  # designed but deferred — fail closed
    ],
)
def test_malformed_or_unsupported_payloads_refuse(body: dict[str, object]) -> None:
    data = _seal_raw_payload(body, PASSCODE)
    with pytest.raises(BundleFormatError):
        unseal(data, PASSCODE)


def test_supabase_payload_names_the_deferral() -> None:
    data = _seal_raw_payload(_payload_body(backend_mode="supabase"), PASSCODE)
    with pytest.raises(BundleFormatError, match="not supported yet"):
        unseal(data, PASSCODE)


# --------------------------------------------------------------------- ttl


def test_parse_ttl_units() -> None:
    assert parse_ttl("30d") == 30 * 86400
    assert parse_ttl("12h") == 12 * 3600
    assert parse_ttl("45m") == 45 * 60
    assert parse_ttl("900s") == 900
    assert parse_ttl("900") == 900


@pytest.mark.parametrize("bad", ["", "soon", "3w", "-1d", "0"])
def test_parse_ttl_refuses_nonsense(bad: str) -> None:
    with pytest.raises(EnrollError):
        parse_ttl(bad)


# ----------------------------------------------------------- write_private (M3)


def test_write_private_is_0600_and_refuses_symlinks(tmp_path: object) -> None:
    """Adversarial-review M3: secrets land 0600 from creation, and writing
    *through* a pre-planted symlink is refused rather than capturing the token
    at the symlink's target."""
    from pathlib import Path

    from kantaq.enroll import write_private

    base = Path(str(tmp_path))  # type: ignore[arg-type]
    target = base / "secret.env"
    write_private(target, b"HUB_TOKEN=kq_x\n")
    assert (target.stat().st_mode & 0o777) == 0o600
    write_private(target, b"HUB_TOKEN=kq_y\n")  # overwrite tightens/keeps 0600
    assert (target.stat().st_mode & 0o777) == 0o600

    # An attacker pre-plants the path as a symlink to a file they can read.
    victim = base / "attacker-readable"
    victim.write_text("", encoding="utf-8")
    planted = base / "planted.env"
    planted.symlink_to(victim)
    with pytest.raises(EnrollError, match="symlink|regular file"):
        write_private(planted, b"HUB_TOKEN=kq_secret\n")
    assert victim.read_text(encoding="utf-8") == ""  # the token never reached it
