# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kantaq follows the
release line (v0.0.5 → v0.3) described in the project docs.

## [0.3.0] — 2026-08-15

### Added — Sprint 9: v0.3 release (E14, E15, E20, E25, E09, E29)

The second and final v0.3 sprint: tickets group under target-dated **milestones**,
agents self-schedule **follow-ups** as proposals and read a project's **blocking
path**, an opt-in **content-free notification** closes the async loop so a remote
teammate stops refreshing the Inbox, the self-hosted backend gains object storage
and auto-HTTPS, and the generated MCP config offers **both transports**. Schema
reaches **v18**. The package version is bumped to `0.3.0`; this block was
consolidated from `sprint-9-deliverable.md` at release time rather than
accumulated in `[Unreleased]`, so it also covers the post-sprint work below. The
live Supabase delta for the two new collection sets **is applied**
(`milestones`, `ticket_milestones`, and `follow_ups` verified on the
`ck_sync_events_collection` allowlist), and the **`0.3.0` git tag itself waits on
the maintainer's clean-checkout `docker compose up` walkthrough** (DEBT-41(a)) so
the tag reflects a setup path a stranger has actually run.

- **Milestones** (E14-T2/T3, MOD-20/MOD-02/MOD-09/MOD-12, PR #95): a `milestones`
  collection + a `ticket_milestones` junction (migration `0016`, schema **v16**)
  with junction integrity (UNIQUE, same-project, dedup); `MilestoneService` CRUD
  through the signed sink, `/v1/milestones` REST (Viewer reads, Member+ writes),
  the read-only `milestone_get` MCP tool through the eight checks, and a
  **batched** backlog badge — one `ticket_milestones` SELECT per page, no N+1 at
  the 269-ticket dataset. Flat milestones (nestable deferred); reuses
  `tickets.read`/`tickets.write` rather than minting a grant verb.
- **Follow-ups, propose-first** (E15-T1, MOD-29/MOD-09, PR #98): a `follow_ups`
  collection (migration `0017`, schema **v17**) across the full surface, with
  `follow_up_create/update/complete` writing an **`agent_proposal`** that lands in
  the Inbox — an agent never commits one — and `approve_proposal` branching on
  `diff.kind` through the one apply path. `status` moves only through `complete`,
  never a raw patch.
- **Dependency graph** (E15-T2, MOD-29/MOD-09, PR #101): `dependency_graph_get` +
  `dependency_path_find` fold the `blocks` family out of `ticket_relationships`
  (reusing the v0.1 `_relation_arc`); a 31-node critical path resolves on the
  269-ticket relation set. Acyclic by construction, and a cycle reachable from the
  source returns a structured `cycle_detected` naming the offending nodes rather
  than a looped or partial path (fail closed).
- **Async notifications — content-free, opt-in** (E20-T8/T9 [SEC], MOD-12/MOD-25,
  PRs #100 + #102): an outbound dispatch fires on proposal approve **and** reject
  (and on a conflict mint) to a workspace-configured webhook (the floor) or Slack.
  The payload has **no body field at all** — `{action, ids, actor, deep_link}` is
  the only builder — with bounded retries into a `notification_deadletter` table
  (migration `0018`, schema **v18**; local infrastructure, never synced). Opt-in,
  **default off**, host-only config in `local_settings`, so a sink URL or secret
  never enters the sync stream; `notifications.read/write` sit **off the agent
  ceiling**, so the feature never widens permission. Ships with Settings →
  Notifications and a human-only "Request a decision" nudge on a pending proposal.
  Closes DEBT-36; records D-35.
- **Self-hosted hardening** (E25-T3/T4 [SEC], MOD-28/MOD-23/MOD-05, PR #96): a
  `BlobStore` port with a filesystem default and an **S3-compatible** option behind
  an optional `[s3]` extra (the base install is unchanged), export/import widened
  to the port, and a restore-from-backup-into-S3 smoke whose re-export is
  byte-identical; Caddy auto-HTTPS with HSTS/nosniff, secret-hygiene regressions
  (an auth failure never echoes the presented token), and the cross-adapter
  behavior-parity re-run. Closes DEBT-40; advances DEBT-39 — the audit-range
  endpoint and self-hosted compaction stay deferred.
- **Dual-transport discovery** (E09-T5, MOD-08/MOD-13, PR #99): the snippet
  generator emits an HTTP **and** a stdio config per client; the stdio configs
  launch `kantaq mcp stdio` and need no URL, so they are still offered when the
  HTTP gateway is down. A contract test drives both real resolvers and pins an
  identical session derivation and identical `handle_call` decisions — a denial
  over stdio is byte-for-byte the HTTP decision (D-34).
- **Self-host documentation** (E29-T5, MOD-16/MOD-24, PR #97):
  `docs/setup-self-hosted.md` runs clone → `docker compose up` →
  `HUB_MODE=postgres` → connect a stdio agent → invite teammates → backup/restore,
  with the deep operator detail linked rather than duplicated; the compatibility
  matrix refreshes to v0.3 (Tier-2 stdio **scripted 6/6**, not certified; Tier-3
  moves to Sprint 10+). The new docs gate caught a real fresh-clone bug:
  `.env.self-hosted.example` was gitignored and never committed, so E25's
  documented `cp` step was broken on a clean checkout.

### Added — `kantaq enroll`: encrypted one-shot team onboarding (post-sprint)

A teammate joins with **one file and one passcode** — no token paste, no email, no
SQL (`docs/design/enroll.md`). Not in the Sprint 9 backlog; built after it.

- **The `.kqe` lockbox** (PR #108): an argon2id passcode KDF over a NaCl SecretBox,
  with the KDF parameters riding the header and everything semantic sealed inside
  the ciphertext. Three verbs — `kantaq enroll export / import / revoke` — where
  provisioning reuses `seed_member` + `rotate_token`, join reuses the `whoami` +
  `adopt_owner` path and `ensure_device_identity`, and revoke reuses the
  `revoke_member` cascade. PyNaCl is promoted dev→runtime for the umbrella package
  only. Secret-bearing files are `0600` from the first byte. The **Supabase path is
  designed but deferred** (`docs/design/enroll.md` §6), and remote owner
  provisioning is deferred with it.
- **Two adversarial security passes** (PRs #108 + #109). The first capped the
  **accepted** KDF ceiling at the MODERATE profile we seal with, so a hostile
  `.kqe` cannot make a joiner run a ~1 GiB Argon2id it never asked for; validated
  that `hub_url` is http(s) **inside unseal**, before import fetches it (no
  `file://`, no metadata address); and made `write_private` use `O_NOFOLLOW`,
  refusing to write a token through a pre-planted symlink. The second required
  **https-only** hub URLs off loopback, cut the bundle TTL from 30 days to **48h**
  (Owner bundles 24h), made `enroll import` **delete the `.kqe` on success**
  (`--keep` opts out), added an `.env` guard that auto-gitignores inside a repo and
  warns on cloud-synced directories, and gave `--passcode-file` a `0600` write
  instead of printing the passcode. Deferred with written rationale: a server-side
  one-time nonce, an owner-signed bundle, and OS-keychain at-rest (accepted risk,
  D-06).

### v0.3-close (before the tag — what the self-host walkthrough found)

The DEBT-41(a) clean-checkout walkthrough was run against a real sync-server and
surfaced four defects every hermetic gate had missed. All four are fixed and
live-proven.

- **A self-hosted runtime couldn't push** (DEBT-42, PR #104) [SEC]: `seed` minted a
  member with a server-generated id while the runtime authored events as its own
  first-boot Owner, so caller-binding (`actor == the token's member`) rejected every
  push. A fresh machine hit it too; the E25 compose smoke only passed because the
  test stamped `actor_id`. `kantaq sync login` now resolves the token's member via a
  token-gated, self-scoped `GET /v1/me` and creates the local Owner **as** that
  member — idempotent, and it refuses to re-home a runtime that already carries a
  different identity. `sync once` guards up front instead of failing per-event.
- **`kantaq dev` wouldn't boot in postgres mode** (DEBT-43, PR #105):
  `verify_connection` had no `postgres` branch, so a self-hosting user couldn't run
  the runtime or web UI at all. Live-proven end to end: `db migrate` →
  `sync login` → `dev --check` → `sync once` = **1 committed**.
- **Agent-authored events could never sync** (PR #107): the acting-member resolver
  counted every same-email row, so a manifested Agent member tripped the "more than
  one workspace" refusal — and, atomic-reject, poisoned the owner's own events with
  `policy_denied`. The resolver now skips `role=Agent` rows and refuses only on more
  than one *distinct workspace*; sync status counts pending by what flush actually
  drains, and surfaces terminal rejected/rebase-required rows as **parked** instead
  of "awaiting push" forever.
- **A second member's first pull wedged** (DEBT-45, PR #110): member rows were
  created directly by seed/adopt and never entered the event log, but
  `devices.member_id` and `capability_grants.subject` FK into `members` — so the
  moment a peer pulled another's device event, the trust-root fold died on
  referential integrity. Members now **distribute as events**: a boot self-announce
  emits the runtime's own member row before its device event, a trust-root ingest
  guard folds a legacy stream through a placeholder that the real announce later
  heals in place, and a verify carve-out keeps a self-announce verifiable
  post-cutover. Proven with a live two-member compose smoke converging both ways.

### Changed

- **The web UI is tokenized, with dark mode** (PR #106, MOD-11/MOD-12): every
  color, font, radius, and shadow routes through one token source — no component
  hardcodes a design value — and a persisted light/dark theme defaults to the OS
  preference, applied before first paint so there is no flash. Addresses DEBT-38.

## [0.2.0] — 2026-06-18

### Added — Sprint 7: v0.2 release (E05, E06, E07, E17, E20, E23, E26, E27, E29)

The second and final v0.2 sprint: the offline conflict engine is finished, grants
are backend-issued with sub-5-second revocation, retention holds the cost ceiling,
the metrics dashboard and conflict review ship, a real Linear export re-imports in
CI, and the v0.2 docs are live. Schema reaches **v15**. The package version is
bumped to `0.2.0` and this block is cut from `[Unreleased]`; the **`0.2.0` git tag
itself waits on the maintainer's live-schema apply** (DEBT-25 Step-B `REVOKE` + the
E06/E07 backend deltas + the live timed/retention smokes, DEBT-30) so the tag
reflects the deployed state.

### v0.2-close (before the tag — UAT + persona-study fixes)

- **Importer CLI hardened** (DEBT-33): `kantaq import linear` no longer crashes on
  the success path (`DetachedInstanceError` from a post-session print) and reuses a
  same-named target project instead of orphaning an empty duplicate per run; adds a
  CLI-path test (the unit tests only exercised `import_linear`).
- **GUI honesty pass** (DEBT-34): the Settings → Export button is wired to the
  shipped `/v1/export` (downloads `kantaq-export.tar.gz`); the disconnected Backlog
  + Settings surface the literal `kantaq token show` command (copy button) and
  validate token shape so a Supabase key is rejected by name instead of silently
  401ing; the Inbox memory-promotions copy now says the loop works via CLI/MCP today
  with the in-Inbox approval GUI landing in v0.3 (was the stale "in a later release
  (v0.2)").

- **Conflict engine finished + the RISK-04 race matrix** (E05-T3/T4, MOD-26/MOD-30,
  PR #59): a stale agent proposal rebases (`rebase_required`), tombstones never
  resurrect, and `resolve_conflict` writes the resolution as a new audited
  compare-and-swap event. The load-bearing fix is a **CAS-reject in `events.sql`**
  (a new `p_cas` arg): a contended write now raises `rebase_required` and **commits
  nothing** (atomic under the per-workspace advisory lock), closing two
  adversarially-found commit-then-flag data-loss holes; the per-field scan is
  factored into `kantaq.event_conflicts()` so the reject can never drift from the
  reported `conflicts[]`. The offline/online/race matrix (N-way partition heal,
  edit-vs-delete, stale-proposal rebase) is deterministic and green — **RISK-04
  closed**. A follow-up (PR #62) fixed the Supabase adapter silently dropping the
  RPC's `conflicts[]` (so a same-field edit minted no `conflict_record` on real
  Supabase). Local-only `event_log.origin_proposal_id` (migration `0013`, schema
  **v13**). Records **D-17** (agent-proposal staleness policy); advances DEBT-25.
- **Backend-issued grants + <5s revocation + signed invite** (E06-T7/T8,
  MOD-06/MOD-08, PR #72): grant issuance is role-aware (agents stay capped at 24 h;
  humans get the lifted v0.2 ceiling, with backend revocation as the control, not a
  short TTL). A **wall-clock timed proof** (`time.monotonic`) revokes a derived
  session and asserts the gateway's live per-call re-check denies sub-second
  (NFR-E06-2) — the cross-replica live-Supabase revocation smoke is owed at the
  maintainer apply (DEBT-30). Signed `twp://invite` bundles (`kantaq_protocol.invites`
  + `POST /v1/invitations` craft/accept) verify against the issuer device root;
  forged / expired / cross-workspace / agent-role / craft-an-Owner invites are
  refused. `capability_grants` window widened INTEGER→BIGINT (migration `0015`,
  schema **v15**). Records **D-21/D-22**; closes **DEBT-04, DEBT-26**.
- **Retention + RFC 6962 Merkle anchors** (E07-T4/T5, MOD-07/MOD-17/MOD-27/MOD-05,
  PR #71): `sync_events` compacts after 30 days **below the min-acked-revision
  watermark** (never wall-clock alone — a replica that fell behind is re-snapshotted,
  never stranded) via pg_cron + a guarded DELETE-only bypass of the append-only
  trigger; detailed MCP audit rows summarize after 30 days, **anchor-gated** (the
  run refuses an unanchored range). Merkle anchors fold the linear hash chain into
  O(log n) proofs (RFC 6962 `0x00`/`0x01` domain-separated hashing on stdlib
  `hashlib` — no Python Merkle library cleared the golden-rule bar). `audit_anchors`
  collection (schema **v14**, append-only, off the sync allowlist). Closes the
  FR-E07-5 prereq of the audit-summary half of retention.
- **Recommendation eval** (E17-T6, MOD-22): the 30-fixture confusion matrix
  (TP=51 / FP=0 / FN=0 / TN=69, precision/recall/accuracy = 1.000), the
  recommendation contract-shape pin, and the user-mapping-reflected test —
  confirmed green for the v0.2 close-out (the substance shipped in E17-T3/T5,
  commit `3dfd539`).
- **Conflict review + the metrics dashboard** (E20-T5, MOD-12/MOD-26/MOD-27,
  PR #66): the **Inbox → Sync conflicts** tab (renders both candidate values,
  base_rev, the losing actor, the field path; keep-A / keep-B / new-value → the CAS
  `resolve_conflict`) and the **Settings → Sync** metrics dashboard (capacity gauge,
  replica-by-project, the agent-activity table, retention status, and a "View
  billing in Supabase ↗" deep-link — D-16). `GET /v1/conflicts`,
  `POST /v1/conflicts/{id}/resolve`, `GET /v1/metrics/summary` (OpenAPI + TS client
  regenerated); a Playwright e2e resolves a seeded conflict end-to-end. Resolving
  needs `tickets.write`, so an agent never silently resolves a human's conflict.
  Records **D-18** (ride-flagged).
- **Linear importer** (E23-T3, MOD-23, PR #67): `kantaq import linear` maps status
  → lifecycle stage (MOD-20, both terminal statuses → `learn`), Parent →
  `Ticket.parent_id`, and comments/threads → the activity feed; idempotent on a
  domain-separated `(workspace, kind, linear_id)` id. The synthetic JobWinAI-shaped
  fixture imports clean (269 tickets / 185 relations / 407 comments / 26 `[Epic]`
  parents, every edge case); the **real JobWinAI export smoke** (local, uncommitted
  — DEBT-17) imported the same counts clean and idempotent (re-import 0 new).
  Records **D-19**.
- **Workspace metrics & retention estimator** (E26-T1, MOD-27, PR #65):
  `core.metrics.summary()` (counts, replica size by project, per-actor agent
  observability, the **non-dollar** capacity gauge vs the Free 500 MB / 5 GB
  ceilings, retention status) lands the rows/bytes estimate **within 10% of
  `pg_total_relation_size`** (−1.76% on the seeded 394,535-row profile);
  `core.retention.run()` refuses unanchored ranges and reports the safe watermark.
  `est_tokens` is fed by the MOD-08 gateway payload-byte tally (PR #70), labelled a
  payload-size proxy, not the agent's model tokens. Records **D-20**; the dollar
  bill stays in the Supabase console (D-16).
- **Full conformance suite + export round-trip CI gate** (E27-T5,
  MOD-15/MOD-17/MOD-23, PR #68): a signed event round-trips client A → backend →
  client B **verified at every hop, for every syncable collection**; the export
  round-trip (+ incremental `?since=cursor`, + the Linear-imported round-trip) is an
  automated gate. Each is proven by a deliberately-failing fixture; a coverage check
  fails loudly if a syncable collection is added without a case. CI stays under
  10 minutes.
- **v0.2 docs + the cost-model post** (E29-T4, MOD-16, PR #69): the **"what a
  4-person team actually pays"** cost-model post (Free $0 / 500 MB → Pro $25 flat →
  VPS $5–10; `<$10` reachable only via the VPS path, and we say so instead of
  rounding the claim) grounded in the MOD-27 numbers (the ~290 MB measured 6-month
  4-person footprint, the estimator within 1.8%), **`docs/sync.md`** (offline
  reconcile, conflict review, watermark-safe retention; cross-links protocol.md),
  the `portability.md` v0.2 round-trip note, and the `clients/compatibility.md` v0.2
  re-verification (matrix current, last-verified 2026-06-16). The README links both
  new docs; the docs-profile gate (`tests/docs/test_v02_docs.py`) pins the set and
  that the cost claim matches the MOD-27 numbers; the internal-link gate confirms
  every link resolves.
- **DoD test-gap closure** (PR #73): standing `red_team.py` regressions for the E06
  escalation findings (a tampered role can't lift the grant ceiling; an agent can't
  craft a human-tier grant), the Settings → Sync dashboard headless-QA Playwright
  e2e, an idle-pause vitest case, and the sync-cycle retention-wiring test.

### Added — Sprint 6: v0.2 foundations (E24-T6/T7, E13-T4, E17-T4)

- **Atomic commit RPC** (E24-T6, MOD-05, D-09): `supabase/rpc/events.sql` —
  `public.events(...)` commits events in one plpgsql transaction (validate the
  grant against committed state + signature presence, apply LWW-by-commit-order,
  assign the revision, report `stale_base_rev`), serialised per workspace by a
  `pg_advisory_xact_lock` so a reader never sees revision `N+1` before `N`. The
  Ed25519 *byte* check stays client-side at the `VerifyingBackend` edge (stock
  Postgres has no Ed25519); the RPC enforces everything else server-side
  (MOD-17 honest-naming). The adapter gains `SupabaseSyncBackend.commit_events`.
- **Append-only history, even for `service_role`** (E24-T7, MOD-05):
  `supabase/policies/0003_append_only.sql` — a `BEFORE UPDATE OR DELETE` row
  trigger and a `BEFORE TRUNCATE` statement trigger make committed `sync_events`
  immutable past BYPASSRLS (incl. `ON CONFLICT DO UPDATE`).
- **Trust-root ingest** (E24-T7, MOD-05/06): `devices` and `capability_grants`
  join the sync surface (allowlist 9→11, kept in lock-step across the CHECK,
  `SYNCABLE_MODELS`, the README ALTER note, and `NEVER_SYNC`); a broad pull folds
  them without wedging (DEBT-21).
- **Memory promotion workflow** (E13-T4, MOD-19): `draft → proposed → approved`
  via `POST /v1/memory/{id}/promote` + `/approve` + `/reject`. An agent may only
  *propose* (`memory.write`); approval is human-only (new `Action.memory_approve`,
  a compare-and-swap). Promoting a `local` entry copies it to a new `team`
  `proposed` row and leaves the original immutable + unsynced (NFR-E13-1
  re-proven; provenance is id-free).
- **db-backed skill registry** (E17-T4, MOD-22): `skill_containers` +
  `skill_mappings` collections (migration `0010`, schema v10) + the sink-less
  `kantaq_core.skills.SkillRegistryService`; the 29 hardcoded containers are
  seeded behind the same contract. Skill mappings are **descriptive** (DEBT-06
  resolved; DEBT-07 moot). The registry is managed locally (off the sync
  allowlist in v0.2).

## [0.1.0] — 2026-06-14

The v0.1 release: the full hero loop, signed-and-verified sync, the eight Tier-1
compatibility tests (scripted 8/8), the wired v0.1 CI gate set, a red-team
containment proof, lossless export round-trip, and the public documentation set.
The certified-Tier-1 badge (a real GUI client passing all 8 at a pinned version),
the live wall-clock hero demo (real agent + real Supabase, timed under 15 minutes),
and the warm-channel launch posts are the remaining human release steps —
[`docs/clients/compatibility.md`](docs/clients/compatibility.md) tracks the badge
rule, and the launch is staged but not auto-posted.

### Added — Sprint 5: docs & distribution (E29, MOD-16)

- **The published protocol spec** (E29-T2): new
  [`docs/protocol.md`](docs/protocol.md) — entities, the RFC 8785 canonical
  codec (restricted profile), Ed25519 signing with domain separation, capability
  grants and the `verify_grant` order, dedup/`base_rev` idempotency, the audit
  hash chain, merge policies, error codes, and conformance (golden vectors + the
  E27-T4 smoke). The wire contract a second implementation needs to interoperate.
- **Security + MCP docs finalized for v0.1** (E29-T2): `docs/security.md`'s PRD
  §15 control table refreshed to the live state (E06/E07/E08/E09/E13/E24 now
  shipped), plus an Audit section and the wired CI-gate table; `docs/mcp.md`
  catalog re-verified against the live tool set; the whole doc set
  (protocol ↔ security ↔ mcp ↔ compatibility ↔ portability) is now cross-linked.
- **README rewritten for launch** (E29-T2).
- **Docs-profile gates extended** (E29-T2): the new docs are covered by the
  internal-link and command-drift gates, plus a v0.1 "published docs exist and
  are cross-linked" pin. An opt-in `make linkcheck` (lychee) spot-checks external
  URLs at release time; CI stays hermetic.
- **Version bumped to 0.1.0** across every package + the runtime `version`
  endpoint; `uv.lock` regenerated.

### Added — Sprint 5: v0.1 release readiness (E27, E23, E21)

- **Onboarding wizard** (E21-T3, MOD-13/MOD-19): a guided first-run flow
  (connect → first project → connect agent) that also seeds a project-brief
  memory entry so an agent has context on its first run (FR-E21-1). The Backlog
  empty state links into it.
- **v0.1 CI gate manifest** (E27-T3, MOD-15): every gate proven by a
  deliberately-failing fixture — tamper a golden vector, drop the untrusted
  marker, break the eval resolver, expire/rotate a grant, slow the hero flow
  (`tests/test_gate_suite.py`). The hero-flow timing stub becomes a real
  end-to-end timed flow (join → project → agent reads + proposes over MCP →
  human approves → signed change syncs to a second client, under 15 min).
- **Conformance smoke** (E27-T4, MOD-15/MOD-17): one signed event round-trips
  client → backend → second client, verified at every hop, with a one-byte
  tamper proven refused at each hop.
- **Bundle importer + lossless round-trip** (E23-T2, MOD-23): `import_bundle`
  reconstructs an export into a fresh runtime (manifest + signature + per-event
  verification, fail-closed); an automated fixture round-trip proves
  byte-identical event logs, identical snapshots, and verified blob hashes, and
  `scripts/roundtrip_check.py` + `docs/portability.md` document the manual
  procedure. The public `POST /v1/import` endpoint and CI gate stay v0.2
  (DEBT-03).

- **Real-agent compatibility harness** (E11-T2 Tier-1 core, MOD-24):
  `scripts/verify_agent.py` (`make verify-agent`) boots the runtime + MCP gateway
  and drives a real, LLM-backed agent (Claude Code / Codex) headless against the
  gateway, asserting it connected, read a ticket, and proposed (then approves as
  the Owner). Opt-in (a real agent needs auth + network), recorded in
  `docs/clients/compatibility.md`. Codex 0.130.0 verified end to end.

### Fixed

- **Schema alignment (doc↔code audit):** removed the `AuditEvent.source` model
  default (`"app"`) so a direct construct can't silently misattribute an audit row
  (SEC S4; `audit.write` already required `source`). Aligned migrations
  `0005/0007/0009` FK id columns to the model (unbounded `VARCHAR`, matching
  `0001`) and added a **length-aware model↔migration gate**
  (`test_migration_string_lengths_match_models`) that caught 8 `VARCHAR(26)`
  drifts Alembic's SQLite `compare_type` was blind to; the dialect-parity
  fingerprint now includes column length.
- **Flaky Vitest teardown** (DEBT-19): `usePolling` now owns and catches the
  refresh promise, so a poll that fails (a transient network error, or a test
  tearing down its fetch mock mid-interval) no longer surfaces as an unhandled
  rejection.
- **Honest hero-flow gate wording:** clarified that the hero-flow CI gate scripts
  the agent's MCP calls via the real MCP SDK client (kantaq runs no LLM); a real
  LLM-backed agent is verified by the new `make verify-agent` harness.

### Added — Sprint 5: client compatibility (E11, Tier-1)

- **Tier-1 compatibility suite** (E11-T2, MOD-24/MOD-30): the 8 Tier-1 acceptance
  tests (T1–T8, PRD §20.4) run in CI against `FakeAgent` — the official MCP SDK
  client (the library Claude Code and Cursor embed) over the real gateway +
  runtime API (`tests/compat`). `scripts/compat_check.py` reproduces the matrix
  pass rate in one command. **Scripted: 8/8**; the real Claude Code / Cursor
  runs against pinned versions are the manual release step (FR-E11-2).
- **Connection snippets for all three clients** (E11-T2, MOD-13): Settings → My
  Agent and `GET /v1/me/agent-snippet` now generate configs for **Claude Code**
  (`.mcp.json`, `type: http`), **Cursor** (`.cursor/mcp.json`, bare `url`), and
  **Codex** (`~/.codex/config.toml`, `[mcp_servers.kantaq]` with
  `bearer_token_env_var` — the token rides the `KANTAQ_AGENT_TOKEN` env var,
  never the file). Each entry carries `format`/`text`/`setup`; the bare `snippet`
  field stays the Claude Code config for back-compat. No token round-trips
  (NFR-E06-1). Codex connects over the same streamable HTTP and was verified end
  to end by `make verify-agent`.
- **Published compatibility matrix** (E11-T3, MOD-24/MOD-16): `docs/clients/
  compatibility.md` records tier, client version, last-verified date, and pass
  rate, with the README badge rule — advertise a tier only when fully passing
  (FR-E11-4). README gains a Compatibility section + badge.

### Added — Epic E01: Repo & environment bootstrap (v0.0.5)

- **uv workspace** with packages `protocol`, `sync_engine`, `core`, `mcp`, `db`,
  the `local-runtime` app, and an umbrella `kantaq` package that carries the
  version and CLI (FR-E01-1).
- **`kantaq` CLI** + **Makefile** one-command dev loop: `setup`, `dev`, `migrate`,
  `test`, `lint`, `typecheck` (FR-E01-2). `dev` boots FastAPI on `127.0.0.1:3939`
  and serves the built web UI (FR-E01-3).
- **Web app scaffold**: React + Vite + Vitest + Biome, built static and served by
  the runtime (the 5 routes land in E18).
- **CI** (GitHub Actions): `py` (ruff + mypy-strict + pytest), `web` (Biome + tsc +
  build + Vitest), and `fresh-clone` (times a cold `setup → migrate → test` under
  10 min) on every PR and push to `main` (FR-E01-4, NFR-E01-1, NFR-E01-2).
- **Tooling**: ruff, mypy (strict), pytest; Biome, tsc, Vitest; pre-commit hooks
  with conventional-commit lint (FR-E01-5).
- **Project files**: Apache-2.0 `LICENSE`, `NOTICE`, `CONTRIBUTING.md`,
  `.github/FUNDING.yml`, and `docs/stack.md` recording ADR-0001 (FR-E01-6).

Migrations (`kantaq db migrate`) are a stub until Epic E02 / MOD-02.
