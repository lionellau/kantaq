# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kantaq follows the
release line (v0.0.5 → v0.3) described in the project docs.

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
- **README rewritten for launch** (E29-T2) and a wedge post,
  [`docs/blog/we-stopped-paying-for-linear.md`](docs/blog/we-stopped-paying-for-linear.md).
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
