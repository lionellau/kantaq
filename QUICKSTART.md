# kantaq Quickstart

Run kantaq on your own machine in about 10 minutes — solo with zero backend, or
as a team that syncs through one shared Supabase project — and then drive the
**full loop**: create a ticket, sync it to a teammate, let an agent read it over
its own loopback gateway and *propose* a change, and approve that change from the
Inbox.

**The local-first model in one paragraph.** Every person runs their own kantaq:
a single local process bound to `127.0.0.1` that serves the web UI and the
API, plus the loopback MCP gateway your agent connects to. There is no shared
application instance and nobody logs into anybody else's machine. In team mode
the only shared thing is a Supabase project that validates and stores committed
events; each member's local copy pushes to and pulls from it.

## Prerequisites

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/)
- Node ≥ 20 and [`pnpm`](https://pnpm.io/)
- `make`, `git`

## Solo mode (default — no backend, no account, no network)

```bash
git clone https://github.com/lionellau/kantaq.git
cd kantaq
make setup
make migrate
make dev
```

Open <http://127.0.0.1:3939> — the runtime serves the web app from the same
port. The first `make dev` mints your **Owner token** and parks it in a
`0600` file beside the local database. The web pages are open on loopback, but
every API call needs that token, *even from localhost*:

```bash
kantaq token show     # print your bearer token
```

Want data to look at right away? Seed a believable demo workspace:

```bash
kantaq db seed        # a demo workspace, project, and tickets
kantaq doctor         # print resolved config + connection check
kantaq token rotate   # revoke the old token, mint + store a fresh one
```

Solo mode (`HUB_MODE=local`) is the default — you do not need a `.env` file at
all. To customize the port or database path, start from the example:

```bash
cp .env.example .env
```

A solo user gets the agent loop too — skip team mode and jump straight to
[The full loop, end to end](#the-full-loop-end-to-end); on one machine both the
"creator" and the "approver" are you, and there is nothing to sync.

## Team mode (shared Supabase backend)

One maintainer sets up the backend once; every member runs their own local
kantaq pointed at it and syncs explicitly.

**Maintainer (once per team):** follow [docs/setup-supabase.md](docs/setup-supabase.md)
to create the Supabase project, apply the schema and Row Level Security
policies, seed the team manifest (one workspace row + one member row per
teammate, and a Supabase Auth user for each), then share the project URL and the
**anon key** with the team (a password manager is the right channel). Never
share the service-role key — no member machine ever needs it.

**Every member (including the maintainer):**

```bash
git clone https://github.com/lionellau/kantaq.git
cd kantaq
make setup
make migrate
cp .env.supabase.example .env
# edit .env: paste the SUPABASE_URL and SUPABASE_ANON_KEY you were given
kantaq doctor                          # verifies the backend is reachable
kantaq sync login --email you@team.dev # emailed one-time code → keychain session
kantaq sync once                       # one push + pull cycle
make dev                               # serve the UI at 127.0.0.1:3939
```

`kantaq dev` refuses to serve if the backend connection fails, so a typo in
`.env` surfaces immediately with a clear message instead of a half-working app.

**How syncing works in v0.0.5.** Sync is **online and explicit**: `kantaq sync
once` does one push (your committed events to the backend) and one pull (the
team's events to you), resolving ties by the backend's commit order (last writer
wins). The web UI polls your *local* replica every couple of seconds, so the
screens refresh the moment a `sync once` lands new events — run `sync once`
(or, in a second terminal, a simple `while` loop around it) whenever you want to
exchange with the team. A background sync daemon, an offline outbox, and signed
events are later releases (v0.2 / v0.1); v0.0.5 is online-only, one workspace per
member, and unsigned. Check what is waiting to go out with:

```bash
kantaq sync status    # local pending events + cursor position
```

## The full loop, end to end

This is the hero loop — the smallest thing a founding team can use instead of
markdown plus GitHub Issues. It assumes **two members, A and B**, each set up as
above (a solo user plays both parts on one machine and skips every `sync once`).

### 1. A creates a ticket, and it reaches B

Member **A** opens <http://127.0.0.1:3939>, creates a project and a ticket in
the Backlog (or scripts it over `POST /v1/tickets`), then pushes:

```bash
kantaq sync once      # A: push the new ticket to the backend
```

Member **B** pulls, and the ticket appears in B's Backlog (the badge shows it
arrived from sync):

```bash
kantaq sync once      # B: pull A's ticket into B's local replica
```

### 2. B's agent connects to B's own loopback gateway

Member **B** starts the MCP gateway and gives their agent a scoped token.

```bash
make mcp-dev          # or: kantaq mcp dev
```

The gateway binds `127.0.0.1` on a random port, prints its URL, and publishes
it to a `0600` `mcp.json` discovery file beside the local database. To get a
ready-made config, open **Settings → My Agent** in B's web app: it generates the
Claude Code / Cursor HTTP-MCP snippet pointed at **B's own** loopback URL, with
B's token already filled in. Best practice is a dedicated **Agent** member —
**Settings → Members → Invite**, role *Agent* — which mints a token scoped to
exactly `tickets.read` and `proposals.write`. Point your agent's HTTP MCP config
at the snippet. Two tools ship in v0.0.5 — `ticket_get` and
`agent_action_propose`. Full contract: [docs/mcp.md](docs/mcp.md).

You can sanity-check the gateway is up and token-gated from the shell:

```bash
curl -H "Authorization: Bearer $(kantaq token show)" \
  http://127.0.0.1:3939/v1/members
```

### 3. The agent reads the ticket and proposes a change

Through the gateway, the agent calls `ticket_get` (the human-authored fields
come back wrapped in an `<untrusted source="…">` fence, so any instruction
hidden in the ticket text is treated as **data, never executed**), then calls
`agent_action_propose` with, say, a status change and a note.

`agent_action_propose` is **propose-first**: it stores a pending
`agent_proposal` and **does not touch the ticket**. The proposal syncs like any
other record, so after a `sync once` on each side it lands in **both** A's and
B's Inbox.

### 4. A human approves it from the Inbox

Either member opens the **Inbox**, sees the one pending proposal, and clicks
**Approve**. Approval — not the agent — applies the change through the same
validation as any human edit, flips the ticket's status, and writes audit rows.
A `sync once` carries the updated ticket back to the other member.

```bash
kantaq sync once      # both members: exchange the approved change
```

The audit log now shows the **proposer** (the Agent member) and the
**approver** (the human) as two distinct actors — an agent can never approve its
own proposal, because deciding requires `tickets.write` and the Agent token only
holds `proposals.write`. Every MCP call and every human write is recorded.

That is the whole loop: **create → sync → agent proposes → human approves →
sync** — local-first, propose-first, fully audited.

## Members and tokens

A Maintainer or Owner manages teammates from **Settings → Members** in the web
app: invite (pick a role — *Member*, *Viewer*, or *Agent*), list, revoke, and
rotate. A freshly minted token is shown **exactly once** in a dismissable panel
(only its Argon2id hash is stored) — copy it then, or rotate to get a new one.
Revocation takes effect in under 5 seconds.

The same actions are available over the API for scripting; the response carries
the new member's bearer token exactly once:

```bash
curl -X POST -H "Authorization: Bearer $(kantaq token show)" \
  -H "Content-Type: application/json" \
  -d '{"email": "ada@team.dev", "role": "Member"}' \
  http://127.0.0.1:3939/v1/members/invite
```

List, revoke, and rotate work the same way (`GET /v1/members`,
`POST /v1/members/{id}/revoke`, `POST /v1/members/{id}/rotate`). These manage a
member's **local runtime token**; their backend identity (the row that Row Level
Security checks during sync) is seeded once by the maintainer in the Supabase
manifest — see [docs/setup-supabase.md](docs/setup-supabase.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `refusing to start: database schema is not initialized` | `make migrate` |
| `schema version mismatch` after pulling new code | `make migrate` (migrations are rollback-verified) |
| `no runtime token in the keychain` | run `make dev` once — first boot mints it |
| Lost or leaked token | `kantaq token rotate` — the old token is revoked immediately |
| `connection verify failed` in team mode | check `SUPABASE_URL` / `SUPABASE_ANON_KEY` in `.env`; see [docs/setup-supabase.md](docs/setup-supabase.md) |
| `no Supabase session` on `kantaq sync once` | run `kantaq sync login --email you@team.dev` first |
| `no active member row for <email>` | ask the maintainer to add you to the Supabase manifest (members table + Auth user) — see [docs/setup-supabase.md](docs/setup-supabase.md) |
| Proposal not showing up in the other member's Inbox | run `kantaq sync once` on both sides — v0.0.5 sync is explicit |
| Agent snippet says the gateway is down | start it: `make mcp-dev` (or `kantaq mcp dev`) |
| Supabase free project paused after idle | open the Supabase dashboard and restore it (free tier pauses after ~7 days idle) |

The clone-to-green path (`setup → migrate → test`) is enforced by CI on every
PR — a fresh clone stays inside the 10-minute budget.
