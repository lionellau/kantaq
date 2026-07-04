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

## 2. Enroll yourself and join from your runtime

The backend authenticates every write with a normal kantaq **member token** (no
JWT, no RLS — the validator core authorizes each write). The recommended path
is **`kantaq enroll`** — one encrypted bundle + one passcode, no `.env`
editing, no token paste ([docs/design/enroll.md](design/enroll.md)):

```bash
# on the backend host (the container already holds the database connection):
docker compose exec sync-server \
  uv run kantaq enroll export --email you@team.dev --workspace "Acme" \
    --hub-url http://your-host:8889 --out /tmp/enroll-you.kqe
docker compose cp sync-server:/tmp/enroll-you.kqe .
```

`export` provisions the member, seals their token into `enroll-you.kqe`, and
prints a one-time **passcode**. Move the file to the machine that will run
kantaq, then:

```bash
kantaq enroll import enroll-you.kqe    # prompts for the passcode
kantaq sync status                     # prints the hub + negotiated versions
kantaq sync once                       # one push + pull through your server
```

`import` decrypts the bundle, adopts the seeded member as this runtime's
identity, registers your device key as a verification root, and writes the
`HUB_*` lines into `.env` itself. Run it on a **fresh** runtime (it refuses to
re-home one that already has an identity — use a fresh `LOCAL_DB_PATH`).
Delete the `.kqe` after import; it is gitignored either way. When you send a
bundle to someone else, the file and the passcode travel over **different
channels** — a leaked file alone reveals nothing.

<details>
<summary><b>The manual path</b> (what <code>enroll</code> automates — still supported)</summary>

Mint a founding member inside the running container:

```bash
docker compose exec sync-server \
  uv run python -m kantaq_backend_postgres.seed --email you@team.dev --workspace "Acme"
```

It prints a `member:` id and a `kq_…` **token** — copy the token. Set these in
the **runtime's** `.env` — the one at the kantaq repo root, *not* the
`docker/self-hosted-backend/.env` you edited in Step 1:

```
HUB_MODE=postgres
HUB_URL=http://your-host:8889          # or https://your-domain behind Caddy
HUB_TOKEN=kq_...                       # the token the seed command printed
```

Then **join** the backend, verify, and sync:

```bash
kantaq sync login      # join: your runtime adopts the seeded member as its identity
kantaq sync status     # prints the hub + the negotiated protocol versions
kantaq sync once       # one push + pull through your self-hosted server
```

> **Run `kantaq sync login` on a fresh runtime, before `kantaq dev`.** The server
> only accepts events you authored *as the member your token belongs to*, so
> `login` establishes your local identity **as** that member. If a runtime
> already has its own identity, join from a fresh `LOCAL_DB_PATH` instead (the
> command will say so).

</details>

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
and points it at the same `HUB_URL` with their own member token. To add one,
**export an enrollment bundle** on the backend host:

```bash
docker compose exec sync-server \
  uv run kantaq enroll export --email teammate@team.dev --workspace "Acme" \
    --hub-url http://your-host:8889 --out /tmp/enroll-teammate.kqe
docker compose cp sync-server:/tmp/enroll-teammate.kqe .
```

Send the teammate the `.kqe` file and the passcode over **different channels**;
they run `kantaq enroll import enroll-teammate.kqe` and then `kantaq sync once`
— fully set up, no `.env` editing, no token paste, no email, no SQL. Re-running
`export` for the same email **rotates** their credential (the old bundle dies);
`kantaq enroll revoke --email teammate@team.dev` kills their token, grants, and
device roots within the 5 s budget. (The pre-enroll path — `seed` + hand-edited
`.env` + `kantaq sync login` — is in Step 2's manual fold-out, and Settings →
**Members** still invites, lists, revokes, and rotates local runtime tokens.)

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
