# kantaq self-hosted backend (MOD-28)

Run the whole kantaq sync backend yourself — no Supabase — from one
`docker compose up`. The sync-server exposes the **same protocol endpoints** and
runs the **same validators** as the Supabase backend, so your team gets the same
guarantees (signed-event verification, grant authorization, the §8.1 conflict
merge) with your data on your Postgres.

## Quickstart

```bash
cd docker/self-hosted-backend
cp .env.self-hosted.example .env      # then edit POSTGRES_PASSWORD
docker compose up -d                  # sync-server on http://localhost:8889
curl -s localhost:8889/healthz        # {"status":"ok"}
```

That brings up two services: `postgres` (your data) and `sync-server` (the
FastAPI backend, which creates its schema on first boot). For HTTPS, set
`CADDY_DOMAIN` to a public hostname and add `--profile tls`:

```bash
docker compose --profile tls up -d    # Caddy terminates TLS on :443 → sync-server
```

## Point your runtime at it

On each teammate's machine (the one running `kantaq`), set in the runtime's
`.env`:

```
HUB_MODE=postgres
HUB_URL=http://your-server-host:8889    # or https://your-domain behind Caddy
HUB_TOKEN=kqt_<member token>            # a normal kantaq member token
```

Then verify and sync:

```bash
kantaq sync status     # prints the hub + negotiated protocol versions
kantaq sync once       # one push + pull cycle through the self-hosted server
```

## How it stays honest (one validator core, two backends)

The sync-server does **not** reimplement the protocol. It reuses
`verify_event` (grant + Ed25519 signature) and `detect_merge` (the §8.1 conflict
rule) verbatim from the shared sync engine — the same functions the Supabase
backend's plpgsql RPC mirrors and the same the client preview runs. The parity
contract test (`adapters/backend-postgres/tests/test_parity.py`) replays the
golden conflict vectors through this backend and asserts the result equals both
the golden ground truth and `detect_merge`, so the two backends cannot drift.

Because the server is Python (not plpgsql), it additionally verifies the
Ed25519 **signature bytes** server-side — the one check the Supabase RPC cannot
do (no Ed25519 in stock Postgres). The self-hosted server is therefore a strict
superset of the Supabase server-side posture, never a subset.

Authorization is the validator core, not database policy: a Bearer **member
token** authenticates the caller (no JWT, no RLS, OIDC deferred — DEBT-14), the
server binds the acting member, and `verify_event` authorises every write.

## Operations

- **Data** lives in the `kantaq-pg-data` volume. Back it up with
  `pg_dump`/`pg_restore` against the `postgres` service (automated backup +
  object storage are the Sprint-9 hardening, MOD-28).
- **Logs**: `docker compose logs -f sync-server`.
- **Upgrade**: `git pull`, then `docker compose up -d --build`.
- **Reset** (destroys data): `docker compose down -v`.
