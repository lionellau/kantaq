# kantaq Quickstart

Run kantaq on your own machine in about 10 minutes — solo with zero backend, or
as a team that syncs through one shared Supabase project.

**The local-first model in one paragraph.** Every person runs their own kantaq:
a single local process bound to `127.0.0.1` that serves the web UI and the
API, plus the loopback MCP gateway your agent connects to. There is no shared
application instance and nobody logs into anybody else's machine. In team mode
the only shared thing is a Supabase project that validates and stores committed
events; each member's local copy syncs against it.

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

### Connect your agent (MCP)

```bash
make mcp-dev          # or: kantaq mcp dev
```

The gateway binds `127.0.0.1` on a random port, prints its URL, and publishes
it to an `mcp.json` discovery file beside the local database. Point your
agent's HTTP MCP config at that URL with `Authorization: Bearer <token>`
(`kantaq token show`, or a scoped Agent member token). Two tools ship in
v0.0.5 — `ticket_get` and `agent_action_propose` (propose-first: an agent
never changes a ticket; you approve from the Inbox). Full contract:
[docs/mcp.md](docs/mcp.md).

```bash
curl -H "Authorization: Bearer $(kantaq token show)" \
  http://127.0.0.1:3939/v1/members
```

Useful follow-ups:

```bash
kantaq db seed        # a believable demo workspace, project, and tickets
kantaq doctor         # print resolved config + connection check
kantaq token rotate   # revoke the old token, mint + store a fresh one
```

Solo mode (`HUB_MODE=local`) is the default — you do not need a `.env` file at
all. To customize the port or database path, start from the example:

```bash
cp .env.example .env
```

## Team mode (shared Supabase backend)

One maintainer sets up the backend once; every member runs their own local
kantaq pointed at it.

**Maintainer (once per team):** follow [docs/setup-supabase.md](docs/setup-supabase.md)
to create the Supabase project, then share the project URL and the **anon key**
with the team (a password manager is the right channel). Never share the
service-role key — no member machine ever needs it.

**Every member (including the maintainer):**

```bash
git clone https://github.com/lionellau/kantaq.git
cd kantaq
make setup
make migrate
cp .env.supabase.example .env
# edit .env: paste the SUPABASE_URL and SUPABASE_ANON_KEY you were given
kantaq doctor         # verifies the backend is reachable before serving
make dev
```

`kantaq dev` refuses to serve if the backend connection fails, so a typo in
`.env` surfaces immediately with a clear message instead of a half-working app.

> **v0.0.5 status — read this.** Team mode currently verifies the backend
> connection and runs everything locally. The Supabase schema, magic-link
> sign-in, Row Level Security, and event sync are the next epics on the
> security spine (E24, then sync in Sprint 2). Until they land, team mode is
> "solo mode plus a verified backend connection" — honest scaffolding, not
> silent magic.

## Members and tokens (team workflow today)

A Maintainer or Owner invites teammates through the API; the response carries
the new member's bearer token **exactly once** (only the Argon2id hash is
stored):

```bash
curl -X POST -H "Authorization: Bearer $(kantaq token show)" \
  -H "Content-Type: application/json" \
  -d '{"email": "ada@team.dev", "role": "Member"}' \
  http://127.0.0.1:3939/v1/members/invite
```

List, revoke, and rotate work the same way (`GET /v1/members`,
`POST /v1/members/{id}/revoke`, `POST /v1/members/{id}/rotate`). Revocation
takes effect in under 5 seconds. The Settings→Members UI for all of this ships
in Sprint 2.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `refusing to start: database schema is not initialized` | `make migrate` |
| `schema version mismatch` after pulling new code | `make migrate` (migrations are rollback-verified) |
| `no runtime token in the keychain` | run `make dev` once — first boot mints it |
| Lost or leaked token | `kantaq token rotate` — the old token is revoked immediately |
| `connection verify failed` in team mode | check `SUPABASE_URL` / `SUPABASE_ANON_KEY` in `.env`; see [docs/setup-supabase.md](docs/setup-supabase.md) |
| Supabase free project paused after idle | open the Supabase dashboard and restore it (free tier pauses after ~7 days idle) |

The clone-to-green path (`setup → migrate → test`) is enforced by CI on every
PR — a fresh clone stays inside the 10-minute budget.
