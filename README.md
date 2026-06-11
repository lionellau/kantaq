# kantaq

**Local-first, agent-native issue tracker for small teams (2–10) who already run a local AI agent** (Claude Code, Cursor, Codex).

Each person clones the repo and runs the app on their own machine. A solo user runs it with zero backend. A team points every member's local copy at one shared sync backend (Supabase, later self-hosted Postgres) that holds committed team state. Agents connect only to each user's loopback MCP gateway and can *propose* changes; a human approves them.

> There is no shared application instance and no server the project operates. The only shared thing is the sync backend — it just validates and stores committed events.

This repository holds the **code**. The product/architecture/planning docs live in [`kantaq-project-docs`](https://github.com/lionellau/kantaq-project-docs).

## Status

`v0.0.5` — Sprint 1 foundation: repo/env (E01), data layer + migrations (E02), local runtime + run modes (E22), web shell (E18), quality gates + shared test harness (E27), identity + token-gated loopback auth (E06). Next on the spine: Supabase backend (E24) and audit (E07).

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
  apps/local-runtime/     FastAPI runtime: REST API + (later) MCP route + serves the UI (MOD-14)
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
| `make migrate` | run DB migrations (stub until E02) |
| `make test` | `kantaq test` → pytest + Vitest |
| `make lint` | ruff + Biome |
| `make typecheck` | mypy + tsc |

See [`docs/stack.md`](docs/stack.md) for the stack decision record (ADR-0001) and tool licenses.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). In short: conventional commits, the *Golden rule* (reuse before build), and every change ships with tests and an updated module spec.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
