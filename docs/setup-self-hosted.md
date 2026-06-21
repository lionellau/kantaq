# Self-host kantaq

Run the entire kantaq sync backend yourself — **no Supabase**. Your team's data
lives on your own Postgres, and you get the same guarantees as the hosted path:
every change is Ed25519-signed and verified, every grant is authorized, and
offline conflicts are detected by the same §8.1 merge rule. The self-hosted
server reuses the exact same validators as the Supabase backend, so a protocol
rule that holds on one holds on both.

This page is the **front door** — the narrative path from nothing to a running
team backend with an agent connected. Every operator knob (compose internals,
TLS, secret hygiene, the full backup commands) lives in the operator reference,
[docker/self-hosted-backend/README.md](../docker/self-hosted-backend/README.md);
this guide links into it rather than repeating it.

> **New to kantaq?** Run [QUICKSTART.md](../QUICKSTART.md) first — solo mode needs
> **zero backend** and walks the full create → propose → approve loop in ~10
> minutes. Come back here when you want a *team* backend you control instead of
> Supabase.

## What you'll have at the end

- a `sync-server` + Postgres you control, from one `docker compose up`;
- your local runtime pointed at it (`HUB_MODE=postgres`) and syncing;
- an AI agent connected over **stdio**, propose-first;
- teammates invited, each running their own local copy against your backend.

## Before you start

- **Docker + Docker Compose** on the host that will run the backend.
- The **kantaq runtime** on each member's machine (the `kantaq` CLI from
  [QUICKSTART.md](../QUICKSTART.md)).
- A **hostname + TLS** if anyone connects from another machine — Step 1 turns it
  on. (On a single host, plain loopback is fine.)

## 1. Bring up the backend

```bash
cd docker/self-hosted-backend
cp .env.self-hosted.example .env      # then set a long random POSTGRES_PASSWORD
docker compose up -d                  # postgres + sync-server on http://localhost:8889
curl -s localhost:8889/healthz        # {"status":"ok"}
```

Two services come up: **`postgres`** (your data, in the `kantaq-pg-data` volume)
and **`sync-server`** (the FastAPI backend, which creates its schema on first
boot).

Serving teammates beyond `localhost`? Turn on HTTPS — set `CADDY_DOMAIN` to a
public hostname and add the `tls` profile:

```bash
docker compose --profile tls up -d    # Caddy terminates TLS on :443 → sync-server
```

Caddy obtains and renews a Let's Encrypt certificate automatically and adds HSTS.
The full TLS and secret-hygiene posture is in the
[operator reference](../docker/self-hosted-backend/README.md#tls--secret-hygiene-hardening-e25-t4).

## 2. Mint a token and point your runtime at it

The backend authenticates every write with a normal kantaq **member token** (no
JWT, no RLS — the validator core authorizes each write). Mint a founding one on
the backend host:

```bash
python -m kantaq_backend_postgres.seed --email you@team.dev --workspace "Acme"
```

Then, in the **runtime's** `.env` (the machine running `kantaq`):

```
HUB_MODE=postgres
HUB_URL=http://your-host:8889          # or https://your-domain behind Caddy
HUB_TOKEN=kq_...                       # the token the seed command printed
```

Verify the connection and run one cycle:

```bash
kantaq sync status     # prints the hub + the negotiated protocol versions
kantaq sync once       # one push + pull through your self-hosted server
```

## 3. Connect an agent over stdio

Self-hosting pairs naturally with a launch-on-demand agent (Codex) over
**stdio** — the gateway speaks MCP over the process's stdin/stdout, binds **no
socket**, and the token rides the environment. It runs the *same* eight gateway
checks as the HTTP transport; a denial over stdio is byte-for-byte the decision
it is over HTTP.

```bash
kantaq mcp stdio
```

Wire Codex to spawn it — the bearer stays out of the config file and rides an
env var:

```toml
[mcp_servers.kantaq]
command = "kantaq"
args = ["mcp", "stdio"]
env = { KANTAQ_MCP_TOKEN = "<agent token>", KANTAQ_MCP_GRANT_ID = "<grant id>" }
```

This is the **Tier-2 (Supported)** stdio path — scripted **6/6** green in CI; the
real-Codex pipe run is the matrix's one remaining manual step (see the
[compatibility matrix](clients/compatibility.md)). Prefer HTTP, or running Claude
Code / Cursor? **Settings → My Agent** generates those snippets for your own
loopback gateway; the connection details for every client are in
[docs/mcp.md](mcp.md#connecting), and the stdio specifics are in
[its stdio section](mcp.md#stdio-transport-v03). Give the agent its **own Agent
member** (Step 4) so its token is scoped to read tickets and propose changes —
nothing more.

## 4. Invite your teammates

There is **no shared app instance**. Each teammate runs their own kantaq runtime
and points it at the same `HUB_URL` with their own member token. To add one:

- **From the UI:** Settings → **Members** → **Invite**. Pick a role (*Agent* for
  an AI, *Member* / *Viewer* for people); they get a one-time invite token.
- **From the CLI** on the backend host:
  `python -m kantaq_backend_postgres.seed --email teammate@team.dev` mints a
  member token directly.

Each member then sets the same `HUB_MODE` / `HUB_URL` from Step 2 with their own
`HUB_TOKEN`, and runs `kantaq sync once`. Revoke or rotate access anytime from
Settings → Members (or rotate your own token with `kantaq token rotate`) —
revocation takes effect within the kill-switch budget, and rotating a token also
revokes that member's capability grants, so a leaked token can't be re-paired
with a live grant.

## Notifications — opt-in, rolling out in v0.3

Today the feedback loop is the **Inbox**: an agent's proposal lands there and you
approve or reject it. An **opt-in, content-free** outbound signal — a webhook,
Slack, or email on approve/reject, so async handoff doesn't require polling the
Inbox — is rolling out in v0.3 and wires in here when it ships. By design it
carries only ids and the action, never ticket or memory text, so nothing about
your work leaves your machine.

## Back up and restore

Two complementary backstops (full commands in the
[operator reference](../docker/self-hosted-backend/README.md#backup--restore)):

- **Operational** — a periodic `pg_dump` of the Postgres volume (cron it), plus
  your blob store's own versioning for attachment bytes.
- **Portable** — the signed **export bundle** (Settings → **Export**, or
  `POST /v1/export`) re-imports into a fresh runtime and re-content-addresses
  every blob, so your team can rebuild on any backend. This is the
  leave-any-backend, data-sovereignty guarantee; see
  [docs/portability.md](portability.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| `healthz` not ok / connection refused | `docker compose logs -f sync-server`; is `:8889` reachable from the runtime's host? |
| `kantaq sync once` rejected | `HUB_TOKEN` must be a kantaq member token (`kq_...`), not a Postgres or Supabase key — re-mint with the seed command. |
| agent calls denied | check the token's grant scopes (read + propose); rotation revokes old grants, so re-pair the agent after a rotate. |
| HTTPS certificate errors | `CADDY_DOMAIN` must be a public hostname that resolves to the host; see the operator reference. |

## See also

- [operator reference](../docker/self-hosted-backend/README.md) — every compose
  knob, TLS, secret hygiene, and the backup commands.
- [QUICKSTART.md](../QUICKSTART.md) — solo + Supabase-team setup and the full
  propose → approve loop.
- [docs/mcp.md](mcp.md) — the gateway, its eight checks, and every client snippet
  (HTTP and stdio).
- [docs/clients/compatibility.md](clients/compatibility.md) — which clients are
  tested and the tier they earn.
- [docs/security.md](security.md) — the trust model the gateway enforces.
- [docs/sync.md](sync.md) — offline reconcile and conflict review.
