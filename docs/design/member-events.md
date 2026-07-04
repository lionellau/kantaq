# Members distribute as events (DEBT-45)

Status: IMPLEMENTED in this PR — awaiting the maintainer's design-OK, like
enroll before it. Closes the gap `docs/design/enroll.md` §9 recorded.
Scope: one emission seam + one ingest guard + one verify carve-out. No new
collection, no schema migration, no wire change, no new dependency.

## 1. The gap

A replica's `members` rows were created **directly** — `seed_member` /
`provision_enrollment` on the backend, `adopt_owner` / `bootstrap_owner` on
the replica — and never entered the event log. But two synced collections
carry foreign keys into `members`:

- `devices.member_id → members.id` (the verification-root map), and
- `capability_grants.subject → members.id`.

So the first time replica B pulled founder A's `devices` event, B held no
`members` row for A and the trust-root fold died on referential integrity —
the whole pull transaction, wedged forever. Symmetrically, A pulling B's
device event failed the same way. Two members ⇒ sync is broken; the enroll
live smoke hit exactly this (enroll.md §9), and the manual `seed` +
`sync login` path reproduces it identically — the gap predates enroll.

`members` was already a syncable collection everywhere **except the write
path**: it is in `COLLECTION_META` (`lww`/backend), in the applier's
`DOMAIN_MODELS`, in the self-host backend CHECK (`schema.py`), and in the
Supabase CHECK (`0002_sync_events.sql`). Nothing ever emitted one.

## 2. The fix — three small pieces

### 2a. Boot self-announce (the emission seam)

`ensure_device_identity` — the boot seam that already emits the runtime's
`devices` event — now first ensures the runtime's **own member row** has a
`members` event in the local log, and emits one (`op="patch"`, payload =
`audit.snapshot(member)`, the exact posture of the device emit next to it)
when it does not. Idempotent across boots by a log check, exactly like the
device row check beside it.

- **Self only.** A runtime announces the member it *is* (DEBT-42: the sync
  server binds `actor == the token's member`, and the announce's
  `entity_id == actor_id`). Nobody authors events about other members here.
  This is sufficient: the FK chain only ever needs the member rows of peers
  that have devices/grants — i.e. peers with runtimes — i.e. peers that boot.
- **Ordering for free.** On a fresh runtime the member event is appended
  *before* the device event, so every post-fix stream folds member-then-device
  and never needs the 2b guard.
- **Backfill for free.** An existing runtime's next boot finds a member row
  with no member event and emits one — deployments self-heal with no
  migration, no operator action.

### 2b. Placeholder ingest guard (legacy streams)

Existing streams already hold device/grant events at low revisions whose
member event either arrives much later (after the peer upgrades and reboots)
or never (peer never upgrades). The trust-root ingest
(`kantaq_sync_engine.apply.ingest_trust_root`) therefore satisfies the FK
itself: before folding a `devices`/`capability_grants` row whose member
reference has no local row, it inserts a **placeholder member** — the
referenced id, the replica's (single) workspace, `email=""`, `role="Member"`,
`status="active"` — and the real member event later folds true fields over
it (`_fold_into` updates in place). A placeholder that never gets its event
is cosmetic (an empty-email member in a list), not integrity-bearing: replica
member rows are display/attribution data — authority lives in the backend's
own `members` table and the token verifier, which never fold from events
(§4). The guard also covers the pathological page split (device event and
member event landing in different pull batches).

The domain fold is untouched; only the dedicated trust-root ingest (MOD-26
§B2) gains the guard, and only for its two member-referencing collections.

### 2c. Self-announce verb carve-out (post-cutover correctness)

`verify_event`'s per-verb map requires `members.invite`/`members.revoke` for
`members` events — right for touching **someone else's** row, wrong for a
member announcing **their own**: a plain Member's self-grant carries neither,
so after the signing cutover every self-announce would be `policy_denied`
and legacy-stream joiners would keep placeholder rows forever. The carve-out:
a `members` event whose `entity_id == actor_id` skips the verb check. It
skips **only** the verb check — signature, grant resolution, root
verification, the `grant.subject == actor_id` binding, and workspace scope
all still apply, so the exemption is cryptographically pinned to "you, about
you". Precedent: `devices` (self-registration) is deliberately absent from
the verb map altogether; members self-announce is the same shape with a
tighter condition.

## 3. What this deliberately does not do

- **No member lifecycle sync.** Invites, role changes, and revocations still
  live where they live today (the backend's identity paths; revocation
  enforcement is the token verifier + trust-root revocation, < 5 s). A
  revoked member's peers keep a stale display row until a future lifecycle
  pass; nothing security-bearing reads it (§4). Emitting on every member
  mutation is a natural follow-up, not this fix.
- **No workspace events.** `adopt_owner` guarantees the (single) workspace
  row exists on every enrolled replica before its first pull, so no FK needs
  a workspace event. Workspace-name drift across replicas stays as it is.
- **No per-workspace role reconciliation.** The announce distributes the
  runtime's **local** row, and under the one-identity-per-runtime model
  (`adopt_owner`) every local identity is its runtime's `Owner` — so a peer's
  member list shows runtime-owners, even for a member the backend provisioned
  as `Member`. Display-only (the backend's own rows stay the authority the
  token verifier reads, §4), pre-existing semantics, and squarely
  member-lifecycle territory — flagged for the maintainer rather than
  smuggled into this fix.
- **No backend-side emission.** The backend seeding a member emits nothing;
  the member's own runtime announces on first boot. This keeps every event
  author-signed-able (a server-authored event would break the post-cutover
  "signed by a device grant" invariant — the chicken-and-egg that made
  rows-not-events look attractive in the first place).

## 4. Threat model

- **Escalation via self-announce payload?** A malicious runtime can craft
  `role="Owner"` into its own announce. That row folds into *peers'* replicas
  as display data. It cannot escalate authority: the backend never folds
  events into its identity tables (`app.py` reads `members` for auth from the
  seeded rows only), the token verifier reads the backend's rows, and every
  runtime's own authorization reads its **own** local row (which a peer's
  event cannot become — `adopt_owner`/boot own it). Same trust posture as
  device self-registration today, now stated.
- **Impersonating another member's row?** Pre-cutover: the server's
  caller-binding wall (`actor == token's member`, DEBT-42) rejects an event
  authored as anyone else, and a same-actor event about a *different*
  member's entity is exactly what the verb map still gates post-cutover
  (2c's carve-out requires `entity_id == actor_id`).
- **Secrets on the wire?** The announce payload is `model_dump` of the
  Member row: workspace id, email, role, status, timestamps. No token
  material lives on `members` (tokens are their own local-authority,
  never-synced table). Teammate emails crossing the team's own backend is
  the product's existing posture (member lists already show them).
- **Signing posture.** The boot emit rides the same sink as the device emit:
  unsigned pre-cutover, and when the boot seam gains its signer at the
  cutover, both emits inherit it together. No new unsigned surface is
  introduced; 2c is what keeps the announce verifiable after that cutover.

## 5. Test plan (all red before the fix)

`tests/test_member_event_sync.py` — real replicas, real ASGI sync-server,
Postgres-gated (the DEBT-42 lesson: nothing stamped, nothing faked):

1. **The repro**: founder enrolls + syncs; joiner enrolls + syncs; the
   joiner's pull folds the founder's member row (email/role true) *then* the
   founder's device row. Was: `IntegrityError` on `devices.member_id`.
2. **Symmetry**: one more founder cycle folds the joiner's member + device.
3. **Legacy stream**: founder's members events stripped from the outbox
   before the push (the pre-fix wire, byte-for-byte); the joiner's pull must
   not wedge — placeholder row (`email=""`) satisfies the FK, device folds.

Plus hermetic units: the boot announce is idempotent across boots and
ordered before the device event; the verb carve-out admits self-announce and
still denies a cross-member event without `members.invite`; the placeholder
guard fills and the real event folds over it.

## 6. Rollout

Ship in one PR (this one). No migration, no flag: old runtimes keep working
(their pulls gain the 2b guard with the code update; their next boot
backfills their announce). Mixed fleets converge: an updated joiner can pull
a not-yet-updated founder's stream (2b), and once the founder updates and
reboots, the placeholder heals into the true row (2a + LWW fold).
