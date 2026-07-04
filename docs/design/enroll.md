# `kantaq enroll` — encrypted one-shot onboarding (design)

**Status: DRAFT — awaiting maintainer design OK. Nothing in this PR merges
without it (docs/security.md review gate + this design sign-off).**

The ask (maintainer's words): *"owner sends one encrypted key + a passcode; the
joiner imports it and is fully set up — no `.env` editing, no anon key, no
email, no SQL."*

Today's self-host onboarding ([setup-self-hosted.md §2/§4](../setup-self-hosted.md))
is four manual steps: seed a member on the backend host, copy a `kq_…` token off
a terminal, hand-edit three `.env` lines on the joiner's machine, run
`kantaq sync login`. The token travels in whatever channel the copy-paste used,
in plaintext. Supabase mode is worse ([setup-supabase.md §7](../setup-supabase.md)):
hand-written SQL inserts plus a dashboard auth invite, which that doc itself
defers to "a wizard that drives both sides" in "a later release". `enroll` is
that wizard, for the credential half.

**This is not a new auth system.** Enrollment packages the *existing*
member-token + device-key + capability-grant spine into one encrypted artifact.
The joiner ends up exactly where `seed` + `.env` + `sync login` leaves them
today: a normal member whose runtime adopted the seeded identity and whose
events sign under a grant issued by their own device.

## 1. The three commands

| Command | Runs where | Does |
|---|---|---|
| `kantaq enroll export --email <joiner> --workspace <name> --hub-url <url> [--ttl 48h] [--role Member] [--backend self-host] [--passcode-file <f>]` | the backend host (same place `seed` runs today; needs `KANTAQ_DATABASE_URL` or `--database-url`) | provisions the member + token, seals them into `enroll-<joiner>.kqe`, prints a generated passcode. Remote `hub_url` must be `https` (§11 #4). |
| `kantaq enroll import <file.kqe>` | the joiner's machine | prompts for the passcode, decrypts, verifies the credential against the server (`GET /v1/me`), adopts the identity, ensures the device keypair + verification root, writes `.env` — ready for `kantaq sync once` |
| `kantaq enroll revoke --email <joiner>` | the backend host | revokes the member: every token, every live grant, every device root, audited — propagation < 5 s (NFR-E06-2) |

### Reuse map (nothing reimplemented)

- **Provisioning** — [`seed_member`](../../adapters/backend-postgres/src/kantaq_backend_postgres/seed.py)
  for a new email (workspace + member + Argon2id-hashed token, plaintext
  returned once); [`IdentityService.rotate_token`](../../packages/core/src/kantaq_core/identity/service.py)
  when the member already exists, so **re-export = rotate**: the previous
  bundle's token is revoked in the same transaction that mints the new one.
  Idempotent per (email, workspace); tokens never accumulate. No hand-rolled SQL.
- **Join** — the DEBT-42 path verbatim:
  [`SyncServerBackend.whoami`](../../adapters/backend-postgres/src/kantaq_backend_postgres/client.py)
  resolves who the token is, [`IdentityService.adopt_owner`](../../packages/core/src/kantaq_core/identity/service.py)
  makes the runtime *be* that member (with its fresh-runtime guard intact — a
  runtime that already has a different identity refuses, exactly like
  `kantaq sync login`).
- **Device + roots** — [`ensure_device_identity`](../../apps/local-runtime/src/kantaq_runtime/auth.py):
  keypair into the keychain, verify key registered as a `devices` row through
  the `EventLogSink` seam, so the registration rides the same signed-sync path
  as a boot-time registration (the E04-T4 signer invariant is not bypassed).
- **Revocation** — [`IdentityService.revoke_member`](../../packages/core/src/kantaq_core/identity/service.py),
  which already cascades tokens → grants (`revoke_grants_for_member`) → devices
  in one audited transaction and guards the last Owner. The < 5 s budget holds
  because the sync-server authenticates every request through `TokenVerifier`,
  whose cache TTL is pinned at 3 s (< the NFR-E06-2 budget by construction).

### Placement

Bundle format + flows live in a new `src/kantaq/enroll.py` (the umbrella
package, beside the CLI that is its only consumer). Deliberately **not**
`packages/protocol`: the bundle is a transport lockbox for a credential, not a
protocol object — it signs nothing, verifies nothing, and interoperates with
nothing; putting it in the wire-contract package would suggest otherwise.
`packages/core` stays clean of the new dependency (§3). The PR still trips the
[security review gate](../security.md#the-security-review-gate) — it mints,
moves, and revokes credentials — which is correct and intended.

## 2. The bundle (`.kqe`)

A versioned, self-describing JSON envelope, mirroring the
[portability bundle's](../portability.md) discipline (versioned layout,
fail-closed import, nothing half-applied):

```json
{
  "format": "kantaq-enroll/v1",
  "kdf": "argon2id",
  "salt": "<hex, 16 bytes>",
  "opslimit": 3,
  "memlimit": 268435456,
  "nonce": "<hex, 24 bytes>",
  "ciphertext": "<base64>"
}
```

**The header carries only what key derivation needs.** Everything semantic is
inside the ciphertext:

```json
{
  "format": "kantaq-enroll-payload/v1",
  "backend_mode": "postgres",
  "hub_url": "https://kantaq.acme.example",
  "hub_token": "kq_…",
  "member_id": "<ulid>",
  "member_email": "joiner@acme.dev",
  "workspace_id": "<ulid>",
  "workspace_name": "Acme",
  "issued_at": 1783100000,
  "expires_at": 1785692000
}
```

Consequences, by construction:

- **A tampered header fails closed.** Flip any header field (salt, params,
  nonce) and the derived key or the Poly1305 tag no longer matches —
  decryption raises, import applies nothing. There is no unauthenticated field
  an attacker can usefully alter; `hub_url` redirection, `expires_at`
  extension, and workspace swaps are all under the MAC.
- **A flipped ciphertext byte fails decryption** (XSalsa20-Poly1305 is
  authenticated), never silently corrupts. Asserted by an exhaustive-position
  bit-flip test, same as the protocol's golden-vector discipline.
- **Strict parse.** Unknown envelope keys, unknown `format`, wrong types, or a
  payload whose inner `format` is unrecognized are refused — one spelling per
  statement, the [`decode` philosophy](../protocol.md#2-the-canonical-codec)
  applied to the envelope.
- **Bounded inputs** (the codec's adversarial-hardening rule, applied here):
  the file is capped at 64 KiB, and the header's KDF parameters are capped at
  **exactly the MODERATE profile we seal with** (t=3, 256 MiB), checked *before*
  the KDF runs. The cap is MODERATE, not SENSITIVE, on purpose (adversarial
  review H1): accepting a heavier header bought nothing — `seal` only ever
  emits MODERATE — and would let a hostile file run a ~1 GiB / ~20 s Argon2id
  the joiner never asked for. Capped at MODERATE, the worst an attacker's header
  can cost is exactly what opening a legitimate bundle costs.
- **One spelling per statement, byte-level.** The exhaustive bit-flip test
  found two benign malleabilities on its first run and the format closes both:
  `memlimit` must be a multiple of 1024 (libsodium rounds to 1 KiB granularity,
  so `8192` and `9000` would otherwise derive the same key), and the ciphertext
  must be *canonical* base64 (Python's decoder ignores the final group's
  trailing bits, so a flipped bit there would decode identically). With those,
  the claim is exact: **no flipped bit anywhere in the file still opens.**
- **Timestamps are integer unix seconds**, like grant validity — no datetime
  formatting ambiguity.

What the bundle **never** contains: the Supabase `service_role` key (NFR-E06-1
extends to this artifact), the owner's own token, any other member's token or
email, device private keys, or the passcode (only its salt). The plaintext
`kq_…` token exists in exactly three places, all transient or sealed: the
minting transaction's return value, the sealed ciphertext, and the joiner's
keychain-bound runtime after import. It is never logged and never printed by
`export` (the passcode is printed; the token is not).

## 3. Crypto (vetted primitives, no invention)

- **Passcode → key:** `nacl.pwhash.argon2id.kdf` (libsodium Argon2id),
  32-byte key, random 16-byte salt per bundle, `OPSLIMIT_MODERATE` /
  `MEMLIMIT_MODERATE` (ops 3, mem 256 MiB — libsodium's moderate profile;
  deliberately heavier than the token-at-rest profile because a `.kqe` may sit
  in an email attachment or chat upload for its TTL, an offline-attackable
  artifact in a way the server-side token hash is not). The salt and both
  parameters live in the header, so the profile can be retuned without a
  format break — import reads the header, exactly like PHC strings for tokens.
- **Domain separation:** the KDF input is `b"kantaq:enroll-passcode:v1\x00" +
  passcode`, following the [protocol's domain-tag rule](../protocol.md#domain-separation)
  so this derivation can never collide with any other passcode use.
- **Encryption:** `nacl.secret.SecretBox` — XSalsa20-Poly1305, authenticated,
  random 24-byte nonce, misuse-resistant (the canonical libsodium
  password-lockbox pairing).
- **Passcode generation:** `secrets`-based, six groups of four from a
  31-character unambiguous alphabet (lowercase minus `i/l/o`, digits minus
  `0/1` — ~119 bits) — far past exhaustion even before the memory-hard KDF.
  Owner-supplied passcodes are deliberately not supported in v1: humans pick
  weak ones, and generation removes the temptation.
- **Test profile:** the existing `KANTAQ_ARGON2_TEST_FAST=1` switch (DEBT-18,
  set only by the root `conftest.py`) selects `OPSLIMIT_MIN`/`MEMLIMIT_MIN` so
  the suite is not KDF-dominated. Import is cost-agnostic (parameters ride the
  header), so a test bundle decrypts under any profile and production never
  sets the flag — the same never-weakened contract as
  [tokens.py](../../packages/core/src/kantaq_core/identity/tokens.py).

### Dependency note (maintainer decision point)

PyNaCl (`pynacl>=1.5`) is already pinned in the workspace but **dev-group
only** (the D-11 second-implementation cross-check). This design promotes it to
a runtime dependency **of the umbrella `kantaq` package only** — protocol/core
keep their pyca-`cryptography`-only surface. No new library enters the tree,
and the protocol's production signing stack is untouched.

*Considered and rejected:* pyca `AESGCM` + its `Argon2id` KDF (already a
runtime dep of core) would avoid the promotion — but splits the lockbox across
hand-assembled primitives (salt/nonce/KDF-to-AEAD glue we compose ourselves)
where libsodium ships the exact intended pairing. Happy to swap if you'd rather
keep one crypto library in production; the format is deliberately neutral about
the implementing library (argon2id + an AEAD, parameters in the header).

*Considered and deferred:* an **asymmetric variant** — the joiner runs
`kantaq enroll request`, sends their device **public** key to the owner, the
owner seals with `nacl.public.SealedBox`. No shared passcode at all, and the
bundle is bound to one machine. Costs a round-trip and an extra command in the
joiner's hands before they have kantaq context; the passcode flow is one-shot.
The envelope's `kdf` field is where a `"sealedbox"` mode slots in later without
a format break. v1 ships the passcode flow.

## 4. Two-channel by construction

The `.kqe` file and the passcode are two halves of one credential. `export`
prints exactly this instruction, and the docs repeat it: **send the file and
the passcode over different channels** (file over email/drive, passcode over a
call/DM). A leaked `.kqe` alone is ciphertext under a memory-hard KDF with a
~118-bit passcode: it reveals nothing. A leaked passcode alone is useless
without the file. `.kqe` files are gitignored (this PR adds the pattern) and
the docs say to delete the file after import.

## 5. Threat model

| Threat | Defense | Proven by (regression test) |
|---|---|---|
| `.kqe` leaks without passcode | argon2id(MODERATE) + SecretBox; ~118-bit generated passcode | plaintext-absence test: no payload field (token, url, email) appears in the file bytes; wrong-key decrypt fails |
| passcode leaks without file | passcode is not a credential; salt is per-bundle | (structural — nothing to attack) |
| tampered file (any byte: header or ciphertext) | authenticated decryption fails closed, import applies nothing | exhaustive bit-flip over the envelope must raise; filesystem state asserted unchanged |
| wrong passcode, online guessing | fail closed, bounded interactive attempts (3), non-zero exit; no oracle beyond pass/fail | deliberately-failing fixture: 3 wrong passcodes → exit 1, nothing written |
| replay of an old bundle | `expires_at` (default TTL **48 h**, **24 h** for Owner) checked before any state change; re-export rotates, so a superseded bundle's token is already revoked server-side even inside its TTL; a successful import also **deletes** the `.kqe` (§11 #3) | expired-bundle import refuses; post-rotate old-bundle import decrypts but its token fails `/v1/me` → aborts before adoption |
| bundle for workspace A used against workspace B | the token *is* workspace A's member; the server binds actor == token's member (DEBT-42 wall); import cross-checks `whoami` against the payload's `member_id`/`workspace_id` and aborts on mismatch | existing caller-binding tests + new import-mismatch test |
| joiner's runtime already has an identity | `adopt_owner` refuses to re-home (unchanged) | existing + new CLI-level assertion |
| revoked joiner keeps syncing | `revoke_member` cascade; server-side `TokenVerifier` TTL 3 s | timed test: revoke → old token 401s within the 5 s budget |
| malicious joiner escalates | they receive a normal Member token — every existing wall (roles, grants, RLS-equivalent caller binding) applies; enrollment adds no new authority | existing role/authz suites |
| owner's backend host compromised | out of scope: that host already holds the database (same trust boundary as `seed` today) | — |

## 6. Backends

- **Self-host Postgres (`backend_mode: "postgres"`) — primary, this PR.** The
  credential is a long-lived member bearer token: no SMTP, no session expiry,
  nothing to refresh. This is the backend whose credential model makes
  one-shot enrollment *actually work*.
- **Supabase — designed, deferred (DEBT-43, not in this PR).** Honest problem:
  Supabase auth is email/magic-link; a bundle would carry the project URL +
  anon key + a **session**, and a Supabase session's access token lives ~1
  hour — likely dead before the joiner opens the file. Carrying the refresh
  token instead means a long-lived, silently-renewable credential in a file,
  *plus* the owner must mint it via the admin API (`service_role`-adjacent —
  the key itself must never ride along, NFR-E06-1). The credible design is:
  enroll seeds the §7 manifest rows (workspace/member/agent) via the owner's
  admin credentials *at export time* and the bundle carries only URL + anon
  key + the joiner's email; first `sync login` still does one magic-link
  round-trip. That kills the SQL and the `.env` editing but not the email —
  it's a smaller win and a separate PR. The `backend_mode` field and the
  `--backend` flag ship now (flag value `supabase` exits with a clear
  not-yet-implemented error naming this section), so the file format never
  needs a break.

## 7. Failure modes (each fails closed; each has a test)

wrong passcode (bounded attempts) · tampered file · expired TTL · unknown
format/version · token already rotated/revoked (server 401 at preflight —
before any local write) · payload/`whoami` identity mismatch · runtime already
has a different identity · backend unreachable (network error at preflight,
nothing written) · `.env` exists (merged, `HUB_*` keys updated, other lines
preserved, timestamped backup written first) · re-export for a **revoked**
member refuses (reviving a revoked member is a deliberate admin action, not an
enroll side effect — and a token minted for one would be dead on arrival, since
`TokenVerifier` refuses revoked members) · revoking the last Owner
(`LastOwnerError`, unchanged).

Import's write sequence is: decrypt → validate → **preflight `/v1/me`** →
adopt identity → keychain token → device identity → `.env` — the network and
identity checks come before **any credential, identity, or config is written**,
so a forged/expired/mismatched bundle touches no disk. (The schema-ensure that
follows the checks may create an empty replica file — not secret state — and
every later step is idempotent, so a mid-way refusal like `adopt_owner`'s
re-home guard is fixed by re-running the import.)

## 8. Out of scope (recorded, not hidden)

- **Supabase path** — DEBT-43 (§6).
- **Remote owner provisioning** — `export` runs on the backend host, like
  `seed` today (the operator is the owner in the self-host topology). A
  `POST /v1/members` owner-gated admin surface on the sync-server would let
  the owner export from any machine; that is a deliberate follow-up (DEBT-44)
  because it grows the server's attack surface and deserves its own
  adversarial review, not a rider on this PR.
- **Agent enrollment** — agents keep the existing Settings → My Agent flow
  (their scope ceiling + grant pairing is a different artifact).
- **Notifications/UI** — a Settings → Members "export enrollment" button can
  wrap this CLI later; CLI-first per the maintainer's ask.
- **Cross-member pull of a peer's device event** — DEBT-45 (§9): a pre-existing
  spine gap the live smoke surfaced. The second member's `sync once` *pull*
  fails a FK because member rows never sync as events. Out of scope here (it is
  a protocol/authority change), owned by a follow-up PR; enroll's own surface
  (provision, import/adopt/device/env, single-runtime round-trip, revoke) is
  green.

## 9. Live smoke result + a gap it surfaced (DEBT-45)

The documented self-host smoke ran against the real
`docker/self-hosted-backend` stack (a rebuilt sync-server on `:8889` + its
Postgres, empty DB). **What enroll owns is green:**

- owner `enroll export` (in-container) provisioned two members through
  `seed_member` and sealed two bundles (0600, token never printed — passcode
  only);
- founder `enroll import` on a fresh host runtime: joined + adopted the seeded
  Owner, registered its device as a verification root, wrote `.env` (0600);
- founder created a project + ticket and `kantaq sync once` **committed 3
  events** to the live backend (`cursor 3`);
- joiner `enroll import` on a second fresh runtime joined as a Member and its
  own `sync once` **pushed** its device event (the backend log then held
  `devices: 2`).

**The gap:** the joiner's `sync once` **pull** failed — `FOREIGN KEY
constraint failed` inserting the *founder's* `devices` row, whose `member_id`
has no matching `members` row in the joiner's replica. The backend event log
explains it exactly: `devices: 2, projects: 1, tickets: 1, members: 0`. Member
rows are created by `seed_member` / `adopt_owner` / `bootstrap_owner` /
`invite` as **direct rows, never as synced events**, but the trust-root
`devices` collection *does* sync (teammates need each other's device keys to
verify signed events, E24-T7) — so a peer that pulls another member's device
event has nothing to anchor its FK to.

**This is pre-existing, not enroll's doing.** The device event is emitted by
`ensure_device_identity` — the same call the ordinary `kantaq dev` boot path
makes (`cli.py:_bootstrap_identity`) — so the identical failure reproduces on
the manual `seed` + `kantaq sync login` + `kantaq dev` + `kantaq sync once`
path the moment a *second* real member pulls. The existing E25 compose smoke
and `test_join_identity` never hit it because they drive a **single** runtime
(or stamped actor state) and never pull a *peer's* device event — the same
blind spot DEBT-42 called out ("the compose smoke HID the gap by stamping
`actor_id`; test through a real runtime"). Enroll, by making real two-member
self-host onboarding a two-command affair, is what finally exercised it.

Recorded as **DEBT-45** (a concrete instance of the DEBT-15 "cross-member
device/grant distribution is v0.2" debt): a member must travel over sync
*before* the device that references it. The fix is a spine change — emit
`members` as authoritative events from the provisioning path, or order the
trust-root ingest to backfill the member — and it touches the
identity/authority model (who is authoritative for a member row across
backends), so it belongs to its **own** protocol-reviewed PR, not this
credential PR. Enroll is correct to reuse the existing spine verbatim; the
spine's cross-member sync is the thing to fix next. **Split, and said so**
(per the work rules): design + self-host credential path is this PR; the
member-event distribution fix is the follow-up that makes the second member's
*pull* green.

> **Closed** by the member-event distribution PR
> ([docs/design/member-events.md](member-events.md)): the runtime announces
> its own member row at boot (before the device event), the trust-root ingest
> placeholders a legacy stream's missing member, and the live two-member
> compose smoke that failed above now round-trips green in both directions.

## 10. Open questions for the maintainer (the design-OK checklist)

1. PyNaCl promoted to a runtime dep of the umbrella package only — OK, or
   prefer the pyca composition (§3) and keep PyNaCl dev-only?
2. `export` on the backend host in v1 (same operational posture as `seed`),
   with the remote-admin endpoint as DEBT-44 — agreed?
3. Default enrolled role `Member` (not `Owner`; `--role` overrides) — agreed?
4. TTL default **48 h** (24 h for Owner); `.kqe` extension; `kantaq-enroll/v1`
   format string — any objections?
5. Supabase deferral as DEBT-43 with the §6 design — agreed?
6. **DEBT-45 (§9): the cross-member member-event distribution fix as a separate
   protocol PR** — agree it's out of scope here, or do you want enroll held
   until the second member's *pull* is green too? (My recommendation: land
   enroll now — it is correct and independently valuable for provisioning +
   revocation + single-runtime round-trip — and fix DEBT-45 next, since it is a
   pre-existing spine gap that blocks *all* multi-member self-host sync, not
   just enroll.)

## 11. Second security-review pass — hardening (done) + deferred

A second adversarial pass beyond the initial gate raised nine items. **Six are
fixed in this PR, each with a regression test:**

| # | Item | Fix |
|---|---|---|
| 2 | 30-day TTL was too long for an offline artifact | default **48 h**; `parse_ttl`, `DEFAULT_TTL` |
| 3 | leftover `.kqe` still decrypts a live credential | `enroll import` **deletes** the bundle on success (`--keep` opts out) |
| 4 | a plaintext-`http` remote `hub_url` leaks the token on the wire | `require_secure_hub_url`: **https required off-loopback**, enforced at export *and* real-wire import |
| 6 | `.env` written to cwd could be committed / cloud-synced | `_guard_env_location`: auto-appends `.env` to `.gitignore` in a repo + warns on cloud-sync folders |
| 7 | an Owner bundle has a bigger blast radius | Owner default TTL **24 h**; role-aware in `enroll export` |
| 8 | passcode printed to the terminal (scrollback / CI logs) | `--passcode-file` writes it 0600 instead |

**Three are deliberately deferred (not mechanical fixes — they are design
decisions, and doing them naively would be worse):**

- **#1 true one-time (server nonce, burn-on-import)** — needs a new server-side
  table + a `consume` endpoint + client wiring on the sync-server. A coherent
  backend increment that ripples the schema (the new-collection checklist), so
  it gets its **own** reviewed PR rather than riding a credential PR. Recorded
  as **DEBT-46**. (Today's replay defenses: TTL + rotate-on-re-export +
  revoke + auto-delete.)
- **#5 owner-signed / sealed-box bundle** — the bundle has confidentiality
  (AEAD) but not *origin authentication*: a **self**-signed bundle proves
  nothing (the attacker signs with their own key), so real origin-auth needs
  the joiner to hold the **owner's verify key out-of-band** — a key-distribution
  design decision (TOFU? bundled trust root? the §3 sealed-box variant). Needs
  a maintainer design-OK + its own security review; **DEBT-47**.
- **#9 at-rest token encryption (OS keychain)** — kantaq deliberately chose the
  0600 `FileKeychain` over the OS keychain (a recorded golden-rule re-pass that
  "came back the same", D-06: "anyone with shell access already owns the local
  profile"). Reversing that is a project-wide decision, not enroll's — kept as
  an **accepted risk**, not silently changed.
