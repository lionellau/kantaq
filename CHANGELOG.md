# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kantaq follows the
release line (v0.0.5 → v0.3) described in the project docs.

## [Unreleased]

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
