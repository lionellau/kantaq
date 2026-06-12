"""Canonical codec: determinism, the restricted JCS profile, round-trip (E03-T1).

Crypto profile: property tests prove encode→decode identity and that the
deny paths (floats, big ints, non-string keys, non-canonical input) fail
closed with the structured ``schema_violation`` error.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kantaq_protocol import (
    MAX_SAFE_INT,
    Event,
    SchemaViolation,
    canonicalize,
    decode,
    dedup_key,
    encode_canonical,
    signing_bytes,
)

# ------------------------------------------------------------ scalar values


def test_scalars_render_like_json() -> None:
    assert canonicalize(None) == b"null"
    assert canonicalize(True) == b"true"
    assert canonicalize(False) == b"false"
    assert canonicalize(0) == b"0"
    assert canonicalize(-42) == b"-42"
    assert canonicalize("hi") == b'"hi"'


def test_no_whitespace_and_sorted_keys() -> None:
    assert canonicalize({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_keys_sort_by_utf16_code_units_not_code_points() -> None:
    # "\U0001d306" (TETRAGRAM FOR CENTRE) encodes as the surrogate pair
    # D834 DF06; "０" (FULLWIDTH ZERO) is a single unit FF10. UTF-16
    # order puts the surrogate pair FIRST (D834 < FF10); naive code-point
    # order would put it last (0x1D306 > 0xFF10). RFC 8785 requires UTF-16.
    data = {"０": 1, "\U0001d306": 2}
    encoded = canonicalize(data).decode("utf-8")
    assert encoded.index("\U0001d306") < encoded.index("０")


def test_string_escaping_matches_rfc8785() -> None:
    # Short escapes for the named controls, \u00xx lowercase for the rest,
    # literal UTF-8 for everything printable.
    assert canonicalize('\b\t\n\f\r"\\') == b'"\\b\\t\\n\\f\\r\\"\\\\"'
    assert canonicalize("\x01") == b'"\\u0001"'
    assert canonicalize("é☃") == '"é☃"'.encode()


def test_floats_are_rejected() -> None:
    with pytest.raises(SchemaViolation, match="floats"):
        canonicalize({"x": 1.5})


def test_bool_is_not_an_int_in_the_codec() -> None:
    # bool is an int subclass in Python; it must render true/false, not 1/0.
    assert canonicalize({"flag": True}) == b'{"flag":true}'


def test_ints_beyond_the_safe_bound_are_rejected() -> None:
    assert canonicalize(MAX_SAFE_INT) == str(MAX_SAFE_INT).encode()
    with pytest.raises(SchemaViolation, match="interoperable bound"):
        canonicalize(MAX_SAFE_INT + 1)


def test_non_string_keys_are_rejected() -> None:
    with pytest.raises(SchemaViolation, match="keys must be strings"):
        canonicalize({1: "x"})


def test_unencodable_types_are_rejected() -> None:
    with pytest.raises(SchemaViolation, match="not canonically encodable"):
        canonicalize({"x": object()})


# ----------------------------------------------------------------- property

# JSON values inside the restricted profile (no floats, bounded ints).
_scalars = st.none() | st.booleans() | st.integers(-MAX_SAFE_INT, MAX_SAFE_INT) | st.text()
_json_values = st.recursive(
    _scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=20,
)


@given(_json_values)
def test_canonicalization_is_deterministic(value: object) -> None:
    assert canonicalize(value) == canonicalize(value)


@given(_json_values)
def test_canonical_bytes_reparse_to_an_equal_value(value: object) -> None:
    # decode(encode) identity at the value level: parse the canonical bytes
    # with stock json and re-canonicalize — must be byte-identical.
    encoded = canonicalize(value)
    assert canonicalize(json.loads(encoded.decode("utf-8"))) == encoded


# -------------------------------------------------------------------- events


def _event(**overrides: object) -> Event:
    base: dict[str, object] = {
        "event_id": "01JEVENT000000000000000001",
        "collection": "tickets",
        "entity_id": "01JTICKET00000000000000001",
        "actor_id": "01JDEVICE00000000000000001",
        "actor_seq": 1,
        "payload": {"status": "doing"},
    }
    base.update(overrides)
    return Event(**base)  # type: ignore[arg-type]


def test_event_round_trips_exactly() -> None:
    event = _event(base_rev=7, policy_ref="grant/01JG", sig="ab" * 64)
    assert decode(encode_canonical(event)) == event


def test_none_optionals_are_omitted_not_null() -> None:
    encoded = encode_canonical(_event())
    assert b"base_rev" not in encoded
    assert b"policy_ref" not in encoded
    assert b"sig" not in encoded


def test_signing_bytes_exclude_only_the_signature() -> None:
    unsigned = _event(base_rev=7)
    signed = _event(base_rev=7, sig="ab" * 64)
    assert signing_bytes(signed) == signing_bytes(unsigned)
    assert encode_canonical(signed) != encode_canonical(unsigned)


def test_decode_rejects_unknown_fields() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["surprise"] = 1
    with pytest.raises(SchemaViolation, match="unknown event fields"):
        decode(canonicalize(raw))


def test_decode_rejects_missing_fields() -> None:
    raw = json.loads(encode_canonical(_event()))
    del raw["actor_seq"]
    with pytest.raises(SchemaViolation, match="missing event fields"):
        decode(canonicalize(raw))


def test_decode_rejects_unknown_op() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["op"] = "upsert"
    with pytest.raises(SchemaViolation, match="unknown event op"):
        decode(canonicalize(raw))


def test_decode_rejects_non_canonical_bytes() -> None:
    # Same JSON value, different spelling (whitespace) — refused outright.
    pretty = json.dumps(json.loads(encode_canonical(_event())), indent=2).encode()
    with pytest.raises(SchemaViolation, match="not in canonical form"):
        decode(pretty)


def test_decode_rejects_wrong_types() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["actor_seq"] = "1"
    with pytest.raises(SchemaViolation, match="actor_seq"):
        decode(canonicalize(raw))


def test_empty_required_field_is_rejected() -> None:
    with pytest.raises(SchemaViolation, match="non-empty"):
        encode_canonical(_event(event_id=""))


def test_dedup_key_is_actor_id_and_seq() -> None:
    assert dedup_key(_event(actor_seq=9)) == ("01JDEVICE00000000000000001", 9)


@given(
    st.dictionaries(
        st.text(min_size=1),
        _scalars | st.lists(_scalars, max_size=3),
        max_size=5,
    ),
    st.integers(0, MAX_SAFE_INT),
)
def test_any_in_profile_event_round_trips(payload: dict[str, object], seq: int) -> None:
    event = _event(actor_seq=seq, payload=payload)
    assert decode(encode_canonical(event)) == event


def test_decode_rejects_a_non_object_document() -> None:
    with pytest.raises(SchemaViolation, match="must be a JSON object"):
        decode(b"[1,2]")


def test_decode_rejects_invalid_utf8_and_invalid_json() -> None:
    with pytest.raises(SchemaViolation, match="not valid UTF-8 JSON"):
        decode(b"\xff\xfe")
    with pytest.raises(SchemaViolation, match="not valid UTF-8 JSON"):
        decode(b"{not json")


def test_decode_rejects_non_string_required_fields() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["collection"] = 7
    with pytest.raises(SchemaViolation, match="'collection' must be a string"):
        decode(canonicalize(raw))


def test_decode_rejects_boolean_actor_seq() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["actor_seq"] = True
    with pytest.raises(SchemaViolation, match="'actor_seq' must be an integer"):
        decode(canonicalize(raw))


def test_decode_rejects_non_object_payload() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["payload"] = []
    with pytest.raises(SchemaViolation, match="'payload' must be an object"):
        decode(canonicalize(raw))


def test_decode_rejects_non_integer_base_rev() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["base_rev"] = "7"
    with pytest.raises(SchemaViolation, match="'base_rev' must be an integer"):
        decode(canonicalize(raw))


def test_decode_rejects_non_string_sig() -> None:
    raw = json.loads(encode_canonical(_event()))
    raw["sig"] = 1
    with pytest.raises(SchemaViolation, match="'sig' must be 128 lowercase hex"):
        decode(canonicalize(raw))


# ------------------------------------------------- adversarial hardening


def test_uppercase_sig_spelling_is_rejected_at_decode() -> None:
    # bytes.fromhex would accept "AB..."; one signature must have exactly one
    # byte spelling (E27 adversarial review, must-fix).
    raw = json.loads(encode_canonical(_event(sig="ab" * 64)))
    raw["sig"] = raw["sig"].upper()
    with pytest.raises(SchemaViolation, match="128 lowercase hex"):
        decode(canonicalize(raw))


def test_whitespace_in_sig_is_rejected_at_decode() -> None:
    raw = json.loads(encode_canonical(_event(sig="ab" * 64)))
    raw["sig"] = raw["sig"][:64] + " " + raw["sig"][64:-1]
    with pytest.raises(SchemaViolation, match="128 lowercase hex"):
        decode(canonicalize(raw))


def test_nesting_beyond_the_depth_bound_is_rejected() -> None:
    from kantaq_protocol import MAX_DEPTH

    deep: object = 1
    for _ in range(MAX_DEPTH + 2):
        deep = [deep]
    with pytest.raises(SchemaViolation, match="depth bound"):
        canonicalize(deep)


def test_a_depth_bomb_document_is_refused_not_crashed() -> None:
    bomb = b"[" * 50_000 + b"]" * 50_000
    raw = (
        b'{"event_id":"e","collection":"c","entity_id":"x","actor_id":"a",'
        b'"actor_seq":1,"op":"patch","payload":{"p":' + bomb + b"}}"
    )
    with pytest.raises(SchemaViolation):
        decode(raw)


def test_an_oversized_document_is_refused() -> None:
    from kantaq_protocol import MAX_DOCUMENT_BYTES

    big = b"x" * (MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(SchemaViolation, match="size bound"):
        decode(big)
    with pytest.raises(SchemaViolation, match="size bound"):
        canonicalize("y" * MAX_DOCUMENT_BYTES)


def test_a_huge_integer_literal_is_refused_not_crashed() -> None:
    raw = (
        b'{"event_id":"e","collection":"c","entity_id":"x","actor_id":"a",'
        b'"actor_seq":1,"op":"patch","payload":{"n":' + b"9" * 10_000 + b"}}"
    )
    with pytest.raises(SchemaViolation):
        decode(raw)


def test_lone_surrogates_are_rejected_not_crashed() -> None:
    with pytest.raises(SchemaViolation, match="lone surrogate"):
        canonicalize({"x": "\ud800"})
    with pytest.raises(SchemaViolation, match="lone surrogate"):
        canonicalize({"\udfff": 1})
    # And via decode: JSON that smuggles a lone surrogate in a string.
    with pytest.raises(SchemaViolation):
        decode(
            b'{"actor_id":"a","actor_seq":1,"collection":"c","entity_id":"x",'
            b'"event_id":"e","op":"patch","payload":{"s":"\\ud800"}}'
        )


def test_event_signing_bytes_are_domain_separated() -> None:
    from kantaq_protocol import EVENT_SIGNING_DOMAIN

    assert signing_bytes(_event()).startswith(EVENT_SIGNING_DOMAIN)


def test_sign_refuses_events_decode_would_refuse() -> None:
    # One strict validator across encode/sign/verify (adversarial review):
    # a bool actor_seq or non-object payload cannot be signed either.
    with pytest.raises(SchemaViolation, match="'actor_seq' must be an integer"):
        encode_canonical(_event(actor_seq=True))
    with pytest.raises(SchemaViolation, match="'payload' must be an object"):
        encode_canonical(_event(payload=[1, 2]))
    with pytest.raises(SchemaViolation, match="'event_id' must be a non-empty string"):
        encode_canonical(_event(event_id=123))


def test_empty_policy_ref_is_rejected() -> None:
    with pytest.raises(SchemaViolation, match="'policy_ref' must be a non-empty string"):
        encode_canonical(_event(policy_ref=""))
