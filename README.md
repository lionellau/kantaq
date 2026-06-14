# kantaq

**A local-first, agent-native issue tracker for small teams (2–10) who already run a local AI agent** (Claude Code, Cursor, Codex).

Your tracker runs on your own machine. A solo user runs it with zero backend. A team points every member's local copy at one shared sync backend (Supabase, later self-hosted Postgres) that just validates and stores committed team state. Your AI agent connects to your loopback gateway, reads tickets with the right context, and *proposes* changes — a human approves them from an Inbox diff. Every change is Ed25519-signed and grant-verified before it syncs.

> There is no shared application instance and no server the project operates. The only shared thing is the sync backend — it validates and stores committed events. You can export the whole workspace to a single file and re-import it anywhere, byte-for-byte. Your data is yours.

This repository holds the **code** (Apache-2.0). Product and planning docs are maintained by the core team separately.

**Why we built it:** [we stopped paying for Linear](docs/blog/we-stopped-paying-for-linear.md).

## Status

**`v0.1`** — the first release. The full hero loop works end to end: a fresh member joins, an agent reads a ticket and proposes over MCP, a human approves from the Inbox diff, and the signed change syncs to the whole team. The kantaq side of that loop runs under a **15-minute CI budget** (with the agent's decisions scripted and an in-process backend, so the gate is deterministic); the honest wall-clock run with a real agent and real Supabase is a remaining release step (below). What ships:

- **Local-first sync** — append-only event log, idempotent push/pull, every synced event Ed25519-signed and grant-verified ([docs/protocol.md](docs/protocol.md)).
- **Agent-native, propose-first** — a loopback MCP gateway with eight authorization checks; agents propose, humans approve; nothing an agent does mutates a tracked field directly ([docs/mcp.md](docs/mcp.md)).
- **An honest trust surface** — the Inbox shows proposal diffs with cited memory and a denied-calls tab; the Agents page lists every live session and every denied call straight from the audit log, with revoke + token rotation.
- **Proven boundaries** — a scripted fully-malicious agent session is contained with zero scope escapes; the eight Tier-1 compatibility tests pass 8/8 against the official MCP SDK client in CI; the v0.1 CI gate set is each proven by a deliberately-failing fixture ([docs/security.md](docs/security.md)).
- **Portability** — the workspace exports to one deterministic tarball and re-imports losslessly ([docs/portability.md](docs/portability.md)).

Three human release steps remain: the **certified** Tier-1 badge (a real GUI client passing all 8 at a pinned version), the live wall-clock hero demo (real agent + real Supabase, timed under 15 minutes), and the warm-channel launch posts — see the badge rule below.

## Quickstart

**→ [QUICKSTART.md](QUICKSTART.md)** — solo mode (zero backend) and team mode (shared Supabase, see [docs/setup-supabase.md](docs/setup-supabase.md)). It walks the [full loop end to end](QUICKSTART.md#the-full-loop-end-to-end): create → sync → an agent proposes → a human approves → sync.

```bash
git clone https://github.com/lionellau/kantaq.git
cd kantaq
make setup      # uv sync + pnpm install + build the web UI
make migrate    # database migrations
make test       # pytest + Vitest
make dev        # FastAPI on http://127.0.0.1:3939 serving the built UI
```

A fresh clone reaches green (`setup → migrate → test`) in **under 10 minutes** (NFR-E01-1) — enforced by the fresh-clone CI gate.

## Connect your agent

From Settings → **My Agent**, kantaq generates a connection snippet for your own loopback gateway — `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor), or `~/.codex/config.toml` (Codex). The bearer token is filled client-side and never round-trips through the server. See [docs/mcp.md](docs/mcp.md#connecting).

## Compatibility

[![Tier-1 compatibility: scripted 8/8](https://img.shields.io/badge/Tier--1%20compatibility-scripted%208%2F8-blue)](docs/clients/compatibility.md)

kantaq supports three HTTP MCP clients — **Claude Code**, **Cursor**, and **Codex**. The eight Tier-1 acceptance tests (first connection, role-aware read, propose + approve, permission denial, token rotation, untrusted-content tagging, session expiry, audit completeness) pass **8 / 8 against the official MCP SDK client in CI** (`make compat`), and a **real agent (Codex) connects, reads, and proposes end to end** through the opt-in harness (`make verify-agent`).

Per the badge rule (FR-E11-4), the **Tier-1 (Reference)** badge is advertised as *certified* only once a real client passes all eight at a pinned version — those runs are the release step, recorded with client version and date in the matrix:

**→ [docs/clients/compatibility.md](docs/clients/compatibility.md)** — tiers, the 8 tests, client version, last-verified date, pass rate.

## Documentation

| Doc | What it covers |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | clone-to-running in 10 minutes; solo + team; the full loop |
| [docs/setup-supabase.md](docs/setup-supabase.md) | maintainer-only backend setup (anon key only, never service-role) |
| [docs/protocol.md](docs/protocol.md) | entities, canonical codec, Ed25519 signing, capability grants — the wire contract |
| [docs/security.md](docs/security.md) | threat model, the eight gateway checks, prompt-injection defenses, audit, the review gate |
| [docs/mcp.md](docs/mcp.md) | the MCP gateway, the eight checks, the tool catalog, connection snippets |
| [docs/clients/compatibility.md](docs/clients/compatibility.md) | the Tier-1 matrix and the badge rule |
| [docs/portability.md](docs/portability.md) | export, import, and the lossless round-trip procedure |
| [docs/stack.md](docs/stack.md) | the stack decision record (ADR-0001) and tool licenses |

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
  docs/                   protocol, security, mcp, portability, compatibility, stack ADR
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
| `make compat` | the scripted Tier-1 compatibility runner (8/8) |
| `make verify-agent` | drive a real agent against the gateway (opt-in) |
| `make linkcheck` | spot-check external doc URLs (opt-in; needs `lychee`) |

See [`docs/stack.md`](docs/stack.md) for the stack decision record (ADR-0001) and tool licenses.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). In short: conventional commits, the *Golden rule* (reuse before build), and every change ships with tests and an updated module spec.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
