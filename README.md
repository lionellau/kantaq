# kantaq

**A local-first, agent-native issue tracker for small teams (2–10) who already run a local AI agent** — Claude Code, Cursor, or Codex.

Your tracker runs on your own machine. Solo, it needs **zero backend**. As a team, every member runs their own local copy and points it at one shared sync backend (Supabase today, self-hosted Postgres next) that only validates and stores committed team state. Your AI agent connects to your own private loopback gateway, reads tickets with the right context, and **proposes** changes — you approve them from an Inbox diff. Every change is signed and verified before it syncs.

> No shared app instance, no server to operate, no per-seat bill. The only shared thing is the sync backend. Export your whole workspace to a single file and re-import it anywhere, byte-for-byte. **Your data is yours.**

The full loop — a teammate joins, an agent reads a ticket and proposes a change, a human approves it from the Inbox, and the signed change syncs to everyone — works end to end today.

**Why we built it:** so AI agents can be real contributors — they *propose*, you approve from a diff, and every change is attributed and signed — on a tracker that lives on your own machine.

## Quickstart

Run it on your machine in about 10 minutes:

```bash
git clone https://github.com/lionellau/kantaq.git
cd kantaq
make setup      # install toolchains + build the web UI
make migrate    # set up the local database
make dev        # serve everything on http://127.0.0.1:3939
```

Open <http://127.0.0.1:3939> and you're in. First run mints your Owner token; run `kantaq db seed` to drop in a demo workspace to click around.

**→ [QUICKSTART.md](QUICKSTART.md)** walks it end to end — solo (zero backend) and team (shared Supabase), then the full loop: create a ticket → sync → an agent proposes → you approve → sync.

## Connect your agent

In the web app, open **Settings → My Agent**. kantaq generates a ready-to-paste connection snippet for your own loopback gateway — `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor), or `~/.codex/config.toml` (Codex). Your token is filled in locally and never round-trips through any server.

Best practice: give the agent its own **Agent** member (Settings → Members → Invite, role *Agent*), so its token is scoped to exactly "read tickets" and "propose changes" — nothing more.

→ [docs/mcp.md](docs/mcp.md#connecting)

## What you get

- **Local-first.** Your tracker is one process on `127.0.0.1` serving both the web UI and the API. It works fully offline; sync is an explicit push/pull whenever you want it.
- **Agents propose, humans approve.** Agents reach your tracker only through a loopback gateway that authorizes every call. An agent can open a *proposal* — it can never silently change a ticket. You review the diff in the Inbox and approve, or don't.
- **Safe by default.** Ticket text handed to an agent is fenced as untrusted data, so instructions hidden in a ticket are never executed. Every agent session and every denied call shows up on the Agents page, with one-click revoke that takes effect in under 5 seconds.
- **Made for real teamwork.** Offline edits that collide are detected and surfaced for review instead of silently lost. Your agent draws on shared workspace memory, and memory promotions are human-approved.
- **Yours to keep.** Every synced change is Ed25519-signed and verified. Export the entire workspace to one deterministic file and re-import it losslessly, anywhere.

## Works with

[![Tier-1 compatibility: scripted 8/8](https://img.shields.io/badge/Tier--1%20compatibility-scripted%208%2F8-blue)](docs/clients/compatibility.md)

kantaq speaks HTTP MCP, so it works with **Claude Code**, **Cursor**, and **Codex**. Each is checked against eight Tier-1 acceptance tests — first connection, role-aware read, propose + approve, permission denial, token rotation, untrusted-content tagging, session expiry, and audit completeness.

→ [docs/clients/compatibility.md](docs/clients/compatibility.md) for the current matrix.

## Documentation

| Doc | What it covers |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | clone to running in ~10 min; solo + team; the full loop |
| [docs/setup-supabase.md](docs/setup-supabase.md) | team backend setup — one maintainer, once |
| [docs/mcp.md](docs/mcp.md) | the agent gateway, its checks, the tool catalog, connection snippets |
| [docs/security.md](docs/security.md) | the trust model and how the boundary holds |
| [docs/sync.md](docs/sync.md) | offline reconcile, conflict review, retention |
| [docs/portability.md](docs/portability.md) | export / import and the lossless round-trip |
| [docs/protocol.md](docs/protocol.md) | the wire contract — entities, signing, capability grants |
| [docs/blog/what-a-4-person-team-actually-pays.md](docs/blog/what-a-4-person-team-actually-pays.md) | the real monthly cost: $0 → $25 → a $5–10 VPS |

## Development

kantaq is a `uv` (Python 3.12) + `pnpm` (Node ≥ 20) workspace — a FastAPI runtime, a React UI, and the MCP gateway. The day-to-day:

```bash
make setup      # install + build the UI
make dev        # run the runtime + UI on 127.0.0.1:3939
make migrate    # run database migrations
make test       # run the test suite
```

```
src/kantaq/          the `kantaq` CLI + version
apps/local-runtime/  FastAPI runtime: REST API + serves the UI
packages/            protocol, sync engine, tracker core, MCP gateway, db models
web/                 React + Vite single-page app
adapters/            sync backends (Supabase; self-hosted Postgres next)
docs/                protocol, security, MCP, portability, compatibility
```

The remaining commands (`lint`, `typecheck`, …), the contribution rules, and the stack decision record live in [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/stack.md](docs/stack.md). This repository is the **code** (Apache-2.0); product and planning docs are maintained separately by the core team.

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE).
