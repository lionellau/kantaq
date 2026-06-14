# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); kantaq follows the
release line (v0.0.5 → v0.3) described in the project docs.

## [Unreleased]

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
