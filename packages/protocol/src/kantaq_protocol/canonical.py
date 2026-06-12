"""The canonical byte codec (FR-E03-2, NFR-E03-1) — one codec, or signatures break.

The encoding is **RFC 8785 (JSON Canonicalization Scheme), restricted
profile**: UTF-8, no whitespace, object keys sorted by UTF-16 code units,
JSON string escaping per RFC 8785 §3.2.2.2 — with restrictions that make the
canonical form exactly reproducible everywhere:

- **No floats.** RFC 8785 number serialization requires ECMAScript
  ``Number::toString`` semantics; cross-language float formatting is where
  canonical-JSON implementations historically diverge, so the protocol
  forbids them outright (the Matrix canonical-JSON precedent). Integers are
  allowed within the IEEE-754 exact range (|n| ≤ 2^53 − 1).
- **No lone surrogates** (not encodable as UTF-8; a malleability and crash
  vector), and object keys must be strings.
- **Bounded inputs** (adversarial review): nesting beyond ``MAX_DEPTH`` and
  documents beyond ``MAX_DOCUMENT_BYTES`` are refused as ``SchemaViolation``
  rather than exhausting the recursion or memory budget of a verifier.

Anything outside the profile raises ``SchemaViolation`` — fail closed, never
"best effort" — because two peers that disagree about one byte disagree about
every signature.

Why build instead of reuse (golden rule, recorded in docs/stack.md): no JCS
library clears the 5k-star bar (trailofbits/rfc8785.py, titusz/jcs, the
cyberphone reference are all tiny); the restricted profile is small on
stdlib ``json`` and is pinned by golden vectors plus property tests.

Event wire form: ``encode_canonical`` writes every field of the event,
**omitting optional fields that are None** (a missing field and a null field
are the same statement, so only one spelling may exist). Signature strings
are **strict lowercase hex** — ``bytes.fromhex`` alone would accept
uppercase and whitespace, letting two different byte strings verify under
one signature (adversarial review, must-fix). The signature is computed over
``signing_bytes`` — a domain-separated message (``kantaq:event:v1`` tag plus
the canonical form with ``sig`` removed) — so an event signature can never
be replayed as any other kind of signed object.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from typing import Any

from kantaq_protocol.entities import OPS, Event, Op
from kantaq_protocol.errors import SchemaViolation

# IEEE-754 doubles represent integers exactly only up to 2^53 - 1 (I-JSON /
# RFC 8785 interoperability bound); beyond it JCS peers cannot round-trip.
MAX_SAFE_INT = 2**53 - 1

# Adversarial bounds: protocol objects are small; anything outside these is
# hostile or broken, and a verifier must refuse it cheaply.
MAX_DEPTH = 64
MAX_DOCUMENT_BYTES = 1_048_576  # 1 MiB

# Domain-separation tags: what a key signs is always "<tag> NUL <canonical>",
# so a signature over one object kind can never validate as another.
EVENT_SIGNING_DOMAIN = b"kantaq:event:v1\x00"

# Strict wire encodings — exact length, lowercase only (no +/whitespace/0x).
SIG_HEX = re.compile(r"\A[0-9a-f]{128}\Z")
KEY_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


def _utf16_key(key: str) -> bytes:
    # RFC 8785 §3.2.3: sort property names by their UTF-16 code units.
    # Big-endian bytes compare exactly like code-unit sequences.
    return key.encode("utf-16-be")


def _check_text(value: str) -> str:
    # A lone surrogate survives json.loads but cannot encode as UTF-8 —
    # reject it as schema, not as a crash (adversarial review).
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SchemaViolation(f"string contains a lone surrogate: {exc}") from exc
    return value


def _write(value: Any, out: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise SchemaViolation(f"nesting exceeds the canonical depth bound ({MAX_DEPTH})")
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
        out.append(json.dumps(_check_text(value), ensure_ascii=False))
    elif isinstance(value, list | tuple):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out, depth + 1)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise SchemaViolation(f"object keys must be strings, got {type(key).__name__}")
            _check_text(key)
        out.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_key)):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _write(value[key], out, depth + 1)
        out.append("}")
    else:
        raise SchemaViolation(
            f"type {type(value).__name__} is not canonically encodable "
            "(allowed: null, bool, int, str, list, object)"
        )


def canonicalize(value: Any) -> bytes:
    """Canonical UTF-8 bytes of any in-profile JSON value."""
    out: list[str] = []
    _write(value, out, 0)
    encoded = "".join(out).encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise SchemaViolation(
            f"canonical document exceeds the size bound ({MAX_DOCUMENT_BYTES} bytes)"
        )
    return encoded


def parse_canonical_document(data: bytes) -> dict[str, Any]:
    """Strictly parse canonical bytes into a JSON object (shared deny gate).

    Bounded, structured failures only: size, parse errors, huge numeric
    literals, and pathological nesting all come back as ``SchemaViolation``.
    """
    if len(data) > MAX_DOCUMENT_BYTES:
        raise SchemaViolation(f"document exceeds the size bound ({MAX_DOCUMENT_BYTES} bytes)")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaViolation(f"not valid UTF-8 JSON: {exc}") from exc
    except (RecursionError, ValueError) as exc:  # depth bombs, huge int literals
        raise SchemaViolation(f"document is outside the canonical profile: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaViolation("an encoded protocol object must be a JSON object")
    return raw


def _require_str(raw: dict[str, Any], name: str, *, kind: str) -> None:
    if not isinstance(raw[name], str):
        raise SchemaViolation(f"{kind} field {name!r} must be a string")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ------------------------------------------------------------------- events

_EVENT_FIELDS = tuple(f.name for f in fields(Event))
_REQUIRED_FIELDS = ("event_id", "collection", "entity_id", "actor_id", "actor_seq")


def _validate_event(event: Event) -> None:
    """One strict validator shared by encode, signing, and (via re-encode) decode."""
    for name in ("event_id", "collection", "entity_id", "actor_id"):
        value = getattr(event, name)
        if not isinstance(value, str) or not value:
            raise SchemaViolation(f"event field {name!r} must be a non-empty string")
    if not _is_int(event.actor_seq):
        raise SchemaViolation("event field 'actor_seq' must be an integer")
    if event.op not in OPS:
        raise SchemaViolation(f"unknown event op {event.op!r}; expected one of {OPS}")
    if not isinstance(event.payload, dict):
        raise SchemaViolation("event field 'payload' must be an object")
    if event.base_rev is not None and not _is_int(event.base_rev):
        raise SchemaViolation("event field 'base_rev' must be an integer")
    if event.policy_ref is not None and (
        not isinstance(event.policy_ref, str) or not event.policy_ref
    ):
        raise SchemaViolation("event field 'policy_ref' must be a non-empty string")
    if event.sig is not None and (
        not isinstance(event.sig, str) or SIG_HEX.match(event.sig) is None
    ):
        # Strict lowercase hex: bytes.fromhex would accept uppercase and
        # whitespace, making two canonical byte strings verify under one
        # signature (adversarial review, must-fix).
        raise SchemaViolation("event field 'sig' must be 128 lowercase hex characters")


def _event_mapping(event: Event, *, include_sig: bool) -> dict[str, Any]:
    _validate_event(event)
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
    """What an Ed25519 signature covers: the domain tag + canonical form minus ``sig``."""
    return EVENT_SIGNING_DOMAIN + canonicalize(_event_mapping(event, include_sig=False))


def decode(data: bytes) -> Event:
    """Parse canonical bytes back into an ``Event`` (strict, fail closed).

    Rejects unknown fields, missing required fields, wrong types, malleable
    signature spellings, and non-canonical input (the bytes must re-encode to
    themselves), so a peer cannot smuggle two spellings of one event past a
    signature check.
    """
    raw = parse_canonical_document(data)
    unknown = set(raw) - set(_EVENT_FIELDS)
    if unknown:
        raise SchemaViolation(f"unknown event fields: {sorted(unknown)}")
    missing = {name for name in _REQUIRED_FIELDS if name not in raw} | (
        {"op", "payload"} - set(raw)
    )
    if missing:
        raise SchemaViolation(f"missing event fields: {sorted(missing)}")
    for name in (*_REQUIRED_FIELDS[:4], "op"):
        _require_str(raw, name, kind="event")
    if not _is_int(raw["actor_seq"]):
        raise SchemaViolation("event field 'actor_seq' must be an integer")
    if not isinstance(raw["payload"], dict):
        raise SchemaViolation("event field 'payload' must be an object")

    op: Op = raw["op"]  # validated against OPS by _validate_event below
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
