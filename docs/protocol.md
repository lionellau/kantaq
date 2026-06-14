# kantaq protocol (MOD-17, v0.1)

kantaq is local-first: every member runs the app on their own machine, and the
only shared thing is a sync backend that validates and stores committed events.
The **protocol** is what makes that safe and portable. It is one small,
load-bearing core (`packages/protocol`, the `kantaq_protocol` package): the
entity types, a deterministic canonical byte codec, Ed25519 signing, and
capability-grant verification. Every signature in the system is computed over
*these* bytes — the same codec runs in the sync engine, the backends, the
exporter, and the importer, or signatures break.

This document is the wire contract. It is what a second, independent
implementation would need to interoperate: read a kantaq event log, verify it,
and produce one kantaq accepts. The checked-in **golden vectors**
(`packages/test_harness/fixtures/protocol_golden_vectors.json`) are the
executable form of this spec — if your encoder reproduces them and your verifier
accepts them, you are conformant.

> **Standards honesty.** kantaq implements three published standards and names
> only those: **RFC 8785** (JSON Canonicalization Scheme) for the byte codec,
> **RFC 8032** (Ed25519) for signatures, and **FIPS 180-4** (SHA-256) for the
> audit hash chain. Capability grants are signed with the *same* Ed25519 codec;
> they are **not** JWT, UCAN, or Biscuit and claim none of those semantics. See
> [the golden-rule record](stack.md) for why each was reused or built.

## 1. Entities

All entities are pure values (frozen dataclasses in
[`entities.py`](../packages/protocol/src/kantaq_protocol/entities.py)) — no ORM,
no I/O — so every layer can pass them around without importing storage.
Timestamps that enter a signature (grant validity) are **integer unix seconds,
UTC**: the codec carries no floats and no datetime-formatting ambiguity.

| Entity | What it is | Key fields |
|---|---|---|
| `Actor` | a human member, a **device** runtime, or an agent | `actor_id`, `kind` (`human`/`device`/`agent`), `public_key` (hex Ed25519 verify key — **device actors only**; humans and agents act *through* a device and carry no key), `label` |
| `Collection` | a syncable collection declaration | `name`, `authority_mode` (`local`/`backend`), `merge_policy`, `visibility` (`local`/`team`), `hosting_mode`, `retention_policy` |
| `TeamManifest` | the workspace's self-description: who signs, what syncs | `team_id`, `name`, `actors[]`, `collections[]` |
| `Event` | the unit everything signs, stores, and syncs | `event_id`, `collection`, `entity_id`, `actor_id`, `actor_seq`, `op` (`patch`/`append`/`tombstone`), `base_rev?`, `policy_ref?`, `payload{}`, `sig?` |
| `Snapshot` | a backend fold of one collection at a revision (pull bootstrap) | `collection`, `as_of_rev`, `entities{}` |
| `CapabilityGrant` | a signed permission slip: who may do what, for how long | `grant_id`, `subject`, `issuer` (the **device** that signed it), `resource`, `verbs[]`, `issued_at`, `expires_at`, `revokes?`, `sig?` |
| `BlobRef` | a content-addressed reference to bytes stored outside the event log | `blob_id` (sha256), `filename`, `media_type`, `size_bytes` |
| `AuditAnchor` | a hash-chain anchor over the local audit trail | (see [§6](#6-the-audit-hash-chain)) |

**The device is the only signer (D-01).** A human or agent never holds a key;
their machine's runtime *is* the device actor and signs on their behalf. So
verification always resolves to a device public key — which is why
[`verify`](#3-signing-ed25519) takes the public key explicitly: key resolution
is identity's job (MOD-06), not the codec's.

## 2. The canonical codec

The canonical encoding is **RFC 8785 (JSON Canonicalization Scheme), restricted
profile** ([`canonical.py`](../packages/protocol/src/kantaq_protocol/canonical.py)).
Determinism is the whole point: the same value must produce the same bytes in
Python today, in a second implementation tomorrow, and on the backend — because
those bytes are what gets signed and verified.

The restricted profile:

- **UTF-16 code-unit key sort** and **JCS string escaping**, with **no
  whitespace** (RFC 8785).
- **No floats.** Floats are the cross-language divergence point (the Matrix
  canonical-JSON precedent); they are refused, not silently coerced.
- **Integers bounded** at `|n| ≤ 2^53 − 1` (`MAX_SAFE_INT`, the I-JSON bound) so
  no peer can smuggle a value that another language rounds.
- **Lone surrogates rejected.**
- **Bounded inputs** (adversarial-review hardening): documents over **1 MiB**
  (`MAX_DOCUMENT_BYTES`) or nesting past **depth 64** (`MAX_DEPTH`) are refused
  as `SchemaViolation` — a depth bomb or a huge-int literal never crashes a peer.
- **`None`-valued optional fields are omitted, never `null`.** One statement has
  exactly one spelling.

The functions:

| Function | Returns |
|---|---|
| `canonicalize(value)` | the canonical bytes of any in-profile JSON value |
| `encode_canonical(event)` | the event's full canonical wire form (includes `sig` when present) |
| `signing_bytes(event)` | what gets signed: the domain tag + canonical event **minus `sig`** |
| `decode(bytes) → Event` | strict parse: re-encoding must equal the input, or it is refused |
| `dedup_key(event)` | the idempotency key `(actor_id, actor_seq)` |

Grants have the twin functions `encode_canonical_grant`, `grant_signing_bytes`,
and `decode_grant` with identical strictness.

**`decode` is the security boundary, not a convenience.** It accepts *only*
canonical bytes: it re-encodes what it parsed and rejects the input if the bytes
differ (`"input is not in canonical form"`), and it rejects unknown, missing, or
wrong-typed fields. So a peer cannot present two byte spellings of one
statement and slip a second past a signature check — there is exactly one
spelling, and the signature binds to it.

## 3. Signing (Ed25519)

Signatures are **Ed25519 (RFC 8032)** via
[pyca/cryptography](https://github.com/pyca/cryptography)
([`signing.py`](../packages/protocol/src/kantaq_protocol/signing.py)). Keys and
signatures travel as **strict lowercase hex** — 64 hex chars for a public key,
128 for a signature. (`bytes.fromhex` alone would accept uppercase or
whitespace, which would let two byte strings verify under one signature; that was
an adversarial-review must-fix.)

| Function | Purpose |
|---|---|
| `generate_keypair() → KeyPair` | a new device keypair (the seed is hidden from `repr`) |
| `public_key_of(seed)` | the hex verify key for a seed |
| `sign(event, privkey) → Event` | a copy of the event with `sig` set over its signing bytes |
| `verify(event, pubkey) → bool` | True iff `sig` matches the event's signing bytes |
| `sign_bytes` / `verify_bytes` | the raw primitives (used for grants and the audit chain) |

### Domain separation

Every signature and hash is prefixed with a NUL-terminated domain tag so that a
signature over one kind of object can never be replayed as another:

| Object | Domain tag | Constant |
|---|---|---|
| Event | `kantaq:event:v1\x00` | `EVENT_SIGNING_DOMAIN` |
| Grant | `kantaq:grant:v1\x00` | `GRANT_SIGNING_DOMAIN` |
| Audit-chain link | `kantaq:audit-chain:v1\x00` | `AUDIT_CHAIN_DOMAIN` |

So an event's signing bytes are `kantaq:event:v1\x00` + `canonicalize(event
minus sig)`, and a grant's are `kantaq:grant:v1\x00` + `canonicalize(grant minus
sig)`. Cross-type replay or collision is impossible by construction.

### Golden vectors and dual implementation (D-11)

The conformance fixtures are
`packages/test_harness/fixtures/protocol_golden_vectors.json` (events including a
UTF-16-sort edge with a supplementary-plane key, plus grants). They are
**cross-verified against a second Ed25519 implementation** (PyNaCl/libsodium,
dev-only) at generation time, and `rfc8032_vectors.json` carries the standard's
own §7.1 test vectors — so the vectors are grounded in the RFC, never
self-referential. A flipped byte fails decode or verify; an exhaustive bit-flip
property test proves it.

## 4. Capability grants

A `CapabilityGrant` is a signed permission slip (PRD §6.9): it says **subject**
may perform **verbs** on **resource**, between `issued_at` and `expires_at`,
signed by **issuer** (a device whose public key must be a known root). Grants are
how an agent gets scoped, expiring authority without ever holding a long-lived
credential — the MCP gateway derives a session's scope from the grant (see
[mcp.md](mcp.md#sessions)).

Grants merge as `authoritative_tx` — they are never written optimistically
(MOD-06). Rotation is expressed by `revokes`: a new grant naming the prior one it
replaces.

### `verify_grant` — the offline check

[`verify_grant(grant, roots, *, now, revoked_ids)`](../packages/protocol/src/kantaq_protocol/grants.py)
returns a structured `GrantVerification(ok, reason)` — never a bare bool, so audit
records *why*. `roots` maps issuer-device id → hex verify key; `revoked_ids` is
the **store's** knowledge (a signature cannot prove an absence, so revocation is
an explicit input, MOD-06 supplies it). The checks run in this order, and the
order is deliberate so audit reasons never undersell an attack:

1. structural validation (strict types, hex spellings) → `SchemaViolation`
2. `expires_at ≤ issued_at` → **`invalid_validity`**
3. `sig is None` → **`missing_signature`**
4. issuer not in `roots` → **`unknown_root`**
5. signature does not verify → **`forged`**
6. `grant_id` in `revoked_ids` → **`revoked`**
7. `now < issued_at` → **`not_yet_valid`**
8. `now ≥ expires_at` → **`expired`**
9. otherwise → **`ok`**

**`forged` outranks `revoked` and `expired`**: a tampered grant reports `forged`
even if it is also stale, so the audit log names the worst thing that is true.
Grant validity is integer unix seconds, so no datetime formatting can ever enter
the signed bytes.

## 5. Events, dedup, and idempotency

An `Event` is the atom of change. `op` is `patch` (set fields), `append` (add to
a log-shaped entity, e.g. a comment), or `tombstone` (delete; tombstones are
sticky — they do not resurrect, a v0.2 conflict rule). `payload`
carries the change; `base_rev` is the revision the writer believed it was editing
(the precondition a conflict-sensitive write checks).

**Idempotency** is `dedup_key(event) = (actor_id, actor_seq)`: each actor numbers
its own events monotonically, so a re-push of the same event is a no-op at the
sink, not a duplicate. This is what makes the outbox safe to retry after a crash
or a flaky network.

Merge policies (`MergePolicy`): `lww` (last-writer-wins by revision),
`append_only` (log-shaped, never conflicts), `authoritative_tx` (server-minted,
for grants and v0.2 conflict records), and `crdt` (a stub — `kantaq_protocol.crdt`
returns `policy_not_implemented`; reserved, not claimed).

## 6. The audit hash chain

The audit trail is tamper-evident by the same codec.
[`chain_hash(previous, record)`](../packages/protocol/src/kantaq_protocol/hashing.py)
is one domain-separated SHA-256 link: `kantaq:audit-chain:v1\x00` +
`prev_hex` + `\x00` + `canonicalize(record)`. Binding the prior link's digest
plus the row's content makes a hash-linked log where altering any past row breaks
every link after it. This is the crypto primitive; `kantaq_core.audit` (MOD-07)
orchestrates it (tip lookup, range walk, anchors). See
[security.md](security.md#audit) for how audit is used.

## 7. Errors (the wire vocabulary)

Protocol errors carry a stable `.code` (FR-E03-5) so a peer can branch on the
reason, not a message string:

| Error | `.code` | Meaning |
|---|---|---|
| `StaleBaseRev` | `stale_base_rev` | the event's `base_rev` no longer matches the entity |
| `PolicyDenied` | `policy_denied` | a policy refused the write |
| `SchemaViolation` | `schema_violation` | non-canonical bytes, bad types, or a bound exceeded |
| `UnknownCollection` | `unknown_collection` | the collection is not in the manifest |

## 8. Conformance

Two checked-in proofs make "conformant" testable:

- **Golden vectors** (above): reproduce the canonical bytes, verify the
  signatures, decode-then-re-encode to a fixed point.
- **The conformance smoke** (E27-T4,
  [`tests/test_conformance_smoke.py`](../tests/test_conformance_smoke.py)): one
  signed event round-trips **client → backend → second client**, verified
  (signature **and** grant) at every hop and folded to the delivered state; a
  one-byte tamper is refused at the origin, the backend, and the receiver. The
  [export bundle](portability.md) reuses the exact same `decode`/`verify`/
  `decode_grant` primitives for verified ingest, so a bundle is just an event log
  at rest.

A second implementation is conformant when it produces events this codec decodes
and verifies, and accepts events this codec produces — nothing more.

## See also

- [security.md](security.md) — the threat model, the eight gateway checks, and how signing + grants defend it.
- [mcp.md](mcp.md) — how an agent's session scope is derived from a capability grant.
- [portability.md](portability.md) — the export bundle: a signed event log at rest, and the round-trip proof.
- [clients/compatibility.md](clients/compatibility.md) — which clients are tested against this protocol, and the tier they earn.
