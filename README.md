# kantaq

**Local-first, agent-native issue tracker for small teams (2–10) who already run a local AI agent** (Claude Code, Cursor, Codex).

Each person clones the repo and runs the app on their own machine. A solo user runs it with zero backend. A team points every member's local copy at one shared sync backend (Supabase, later self-hosted Postgres) that holds committed team state. Agents connect only to each user's loopback MCP gateway and can *propose* changes; a human approves them.

> There is no shared application instance and no server the project operates. The only shared thing is the sync backend — it just validates and stores committed events.

This repository holds the **code**. Product, architecture, and planning docs are maintained by the core team separately.

## Status

`v0.0.5` — the walking-skeleton core. On the Sprint 1 foundation (repo/env, data layer + migrations, local runtime + run modes, web shell, quality gates + shared test harness, identity + token-gated loopback auth), Sprint 2 adds the full hero loop: online sync through Supabase (E04/E24), the loopback MCP gateway with propose-first tools (E09/E10), and the screens — backlog, ticket page, Inbox, Members, My Agent (E19/E20/E21). End to end: create → sync → an agent proposes → a human approves → sync. See [QUICKSTART.md](QUICKSTART.md#the-full-loop-end-to-end).

## Compatibility

[![Tier-1 compatibility: scripted 8/8](https://img.shields.io/badge/Tier--1%20compatibility-scripted%208%2F8-blue)](docs/clients/compatibility.md)

kantaq supports three HTTP MCP clients — **Claude Code**, **Cursor**, and **Codex** — each with a generated connection snippet. The eight Tier-1 acceptance tests (first connection, role-aware read, propose + approve, permission denial, token rotation, untrusted-content tagging, session expiry, audit completeness) pass **8 / 8 against the official MCP SDK client in CI** (`make compat`), and a **real agent (Codex) connects, reads, and proposes end to end** through the opt-in harness (`make verify-agent`). Per the badge rule (FR-E11-4), the Tier-1 (Reference) badge is advertised as **certified** only once a real client passes all eight at a pinned version — those runs are the release step, recorded with client version and date in the matrix:

**→ [docs/clients/compatibility.md](docs/clients/compatibility.md)** — tiers, the 8 tests, client version, last-verified date, pass rate.

Connect your agent from Settings → **My Agent**: it generates the snippet for your own loopback gateway — `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor), or `~/.codex/config.toml` (Codex). See [docs/mcp.md](docs/mcp.md#connecting).

## Quickstart

**→ [QUICKSTART.md](QUICKSTART.md)** — solo mode (zero backend) and team mode (shared Supabase, see [docs/setup-supabase.md](docs/setup-supabase.md)).

```bash
git clone https://github.com/lionellau/kantaq.git
cd kantaq
make setup      # uv sync + pnpm install + build the web UI
make migrate    # database migrations
make test       # pytest + Vitest
make dev        # FastAPI on http://127.0.0.1:3939 serving the built UI
```

A fresh clone reaches green (`setup → migrate → test`) in **under 10 minutes** (NFR-E01-1) — enforced by the fresh-clone CI gate.

## Repository layout

```
kantaq/
  pyproject.toml          uv workspace + shared tool config + `kantaq` CLI
  src/kantaq/             umbrella package: version + the `kantaq` CLI
  apps/local-runtime/     FastAPI runtime: REST API + serves the UI (MOD-14)
  packages/
    protocol/             entities, canonical codec, Ed25519, grant verify (MOD-17)
    sync_engine/          event log, snapshots, cursors, push/pull (MOD-04, MOD-26)
    core/                 tracker domain, resolver, recommendations, permissions
    mcp/                  MCP server, gateway checks, sessions, tools (MOD-08, MOD-09)
    db/                   SQLModel models + Alembic migrations (MOD-02)
  web/                    React + Vite SPA (MOD-10..13)
  adapters/               sync backend adapters (Supabase MOD-05, self-hosted MOD-28)
  evals/fixtures/         context-quality eval set (MOD-21)
  docs/                   stack ADR and code-side docs
  .github/workflows/      CI gates
```

## Dev commands

| Command | What it does |
|---|---|
| `make setup` | install both toolchains and build the web UI |
| `make dev` | run the FastAPI runtime on `127.0.0.1:3939` |
| `make migrate` | run DB migrations |
| `make test` | `kantaq test` → pytest + Vitest |
| `make lint` | ruff + Biome |
| `make typecheck` | mypy + tsc |

See [`docs/stack.md`](docs/stack.md) for the stack decision record (ADR-0001) and tool licenses.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). In short: conventional commits, the *Golden rule* (reuse before build), and every change ships with tests and an updated module spec.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
