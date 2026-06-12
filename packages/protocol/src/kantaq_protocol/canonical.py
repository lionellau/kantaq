"""The canonical byte codec (FR-E03-2, NFR-E03-1) — one codec, or signatures break.

The encoding is **RFC 8785 (JSON Canonicalization Scheme), restricted
profile**: UTF-8, no whitespace, object keys sorted by UTF-16 code units,
JSON string escaping per RFC 8785 §3.2.2.2 — with two restrictions that make
the canonical form exactly reproducible everywhere:

- **No floats.** RFC 8785 number serialization requires ECMAScript
  ``Number::toString`` semantics; cross-language float formatting is where
  canonical-JSON implementations historically diverge, so the protocol
  forbids them outright (the Matrix canonical-JSON precedent). Integers are
  allowed within the IEEE-754 exact range (|n| ≤ 2^53 − 1).
- **No floats also means no NaN/Infinity**, and object keys must be strings.

Anything outside the profile raises ``SchemaViolation`` — fail closed, never
"best effort" — because two peers that disagree about one byte disagree about
every signature.

Why build instead of reuse (golden rule, recorded in docs/stack.md): no JCS
library clears the 5k-star bar (trailofbits/rfc8785.py, titusz/jcs, the
cyberphone reference are all tiny); the restricted profile is ~120 lines on
stdlib ``json`` and is pinned by golden vectors plus property tests.

Event wire form: ``encode_canonical`` writes every field of the event,
**omitting optional fields that are None** (a missing field and a null field
are the same statement, so only one spelling may exist). The signature is
computed over ``signing_bytes`` — the same form with ``sig`` removed — so a
signed and an unsigned copy of one event agree about what was signed.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

from kantaq_protocol.entities import OPS, Event, Op
from kantaq_protocol.errors import SchemaViolation

# IEEE-754 doubles represent integers exactly only up to 2^53 - 1 (I-JSON /
# RFC 8785 interoperability bound); beyond it JCS peers cannot round-trip.
MAX_SAFE_INT = 2**53 - 1


def _utf16_key(key: str) -> bytes:
    # RFC 8785 §3.2.3: sort property names by their UTF-16 code units.
    # Big-endian bytes compare exactly like code-unit sequences.
    return key.encode("utf-16-be")


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise SchemaViolation(
                f"integer {value} exceeds the interoperable bound (|n| <= 2^53-1)"
            )
        out.append(str(value))
    elif isinstance(value, float):
        raise SchemaViolation(
            "floats are not canonically encodable (restricted RFC 8785 profile); "
            "represent the value as an integer or a string"
        )
    elif isinstance(value, str):
        # json.dumps emits exactly the RFC 8785 §3.2.2.2 escapes for the
        # no-float profile: \" \\ \b \t \n \f \r, \u00xx (lowercase) for other
        # control characters, everything else literal UTF-8.
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list | tuple):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise SchemaViolation(f"object keys must be strings, got {type(key).__name__}")
        out.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_key)):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise SchemaViolation(
            f"type {type(value).__name__} is not canonically encodable "
            "(allowed: null, bool, int, str, list, object)"
        )


def canonicalize(value: Any) -> bytes:
    """Canonical UTF-8 bytes of any in-profile JSON value."""
    out: list[str] = []
    _write(value, out)
    return "".join(out).encode("utf-8")


# ------------------------------------------------------------------- events

_EVENT_FIELDS = tuple(f.name for f in fields(Event))
_REQUIRED_FIELDS = ("event_id", "collection", "entity_id", "actor_id", "actor_seq")
_OPTIONAL_NONE_FIELDS = ("base_rev", "policy_ref", "sig")


def _event_mapping(event: Event, *, include_sig: bool) -> dict[str, Any]:
    for name in _REQUIRED_FIELDS:
        value = getattr(event, name)
        if isinstance(value, str) and not value:
            raise SchemaViolation(f"event field {name!r} must be non-empty")
    if event.op not in OPS:
        raise SchemaViolation(f"unknown event op {event.op!r}; expected one of {OPS}")
    mapping: dict[str, Any] = {
        "event_id": event.event_id,
        "collection": event.collection,
        "entity_id": event.entity_id,
        "actor_id": event.actor_id,
        "actor_seq": event.actor_seq,
        "op": event.op,
        "payload": event.payload,
    }
    if event.base_rev is not None:
        mapping["base_rev"] = event.base_rev
    if event.policy_ref is not None:
        mapping["policy_ref"] = event.policy_ref
    if include_sig and event.sig is not None:
        mapping["sig"] = event.sig
    return mapping


def encode_canonical(event: Event) -> bytes:
    """The event's full canonical wire form (includes ``sig`` when present)."""
    return canonicalize(_event_mapping(event, include_sig=True))


def signing_bytes(event: Event) -> bytes:
    """What an Ed25519 signature covers: the canonical form minus ``sig``."""
    return canonicalize(_event_mapping(event, include_sig=False))


def decode(data: bytes) -> Event:
    """Parse canonical bytes back into an ``Event`` (strict, fail closed).

    Rejects unknown fields, missing required fields, wrong types, and
    non-canonical input (the bytes must re-encode to themselves), so a peer
    cannot smuggle two spellings of one event past a signature check.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaViolation(f"not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaViolation("an encoded event must be a JSON object")
    unknown = set(raw) - set(_EVENT_FIELDS)
    if unknown:
        raise SchemaViolation(f"unknown event fields: {sorted(unknown)}")
    missing = {name for name in _REQUIRED_FIELDS if name not in raw} | (
        {"op", "payload"} - set(raw)
    )
    if missing:
        raise SchemaViolation(f"missing event fields: {sorted(missing)}")
    for name in (*_REQUIRED_FIELDS[:4], "op"):
        if not isinstance(raw[name], str):
            raise SchemaViolation(f"event field {name!r} must be a string")
    if not isinstance(raw["actor_seq"], int) or isinstance(raw["actor_seq"], bool):
        raise SchemaViolation("event field 'actor_seq' must be an integer")
    if not isinstance(raw["payload"], dict):
        raise SchemaViolation("event field 'payload' must be an object")
    if "base_rev" in raw and (
        not isinstance(raw["base_rev"], int) or isinstance(raw["base_rev"], bool)
    ):
        raise SchemaViolation("event field 'base_rev' must be an integer")
    for name in ("policy_ref", "sig"):
        if name in raw and not isinstance(raw[name], str):
            raise SchemaViolation(f"event field {name!r} must be a string")

    op: Op = raw["op"]  # validated against OPS inside _event_mapping below
    event = Event(
        event_id=raw["event_id"],
        collection=raw["collection"],
        entity_id=raw["entity_id"],
        actor_id=raw["actor_id"],
        actor_seq=raw["actor_seq"],
        op=op,
        base_rev=raw.get("base_rev"),
        policy_ref=raw.get("policy_ref"),
        payload=raw["payload"],
        sig=raw.get("sig"),
    )
    if encode_canonical(event) != data:
        raise SchemaViolation(
            "input is not in canonical form (re-encoding differs); "
            "only canonical bytes are accepted"
        )
    return event


def dedup_key(event: Event) -> tuple[str, int]:
    """The protocol dedup identity (FR-E03-5): ``(actor_id, actor_seq)``."""
    return (event.actor_id, event.actor_seq)
