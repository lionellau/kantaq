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
HUB_TOKEN=kq_<member token>            # a normal kantaq member token
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

## Attachments (object storage)

Attachment **bytes** live in a content-addressed blob store the *runtime* writes
to (the sync-server carries only the tracker rows + their refs). Pick the
backend in each runtime's `.env` (E25-T3, MOD-28):

- **filesystem** (default): bytes sit next to the local replica DB. Simple and
  zero-config, but per-machine — a teammate sees an attachment's bytes only
  after an export/import. Fine for a single host or a "refs sync, bytes are
  local" posture.
- **s3** (shared): point every runtime at one S3-compatible bucket (AWS S3,
  MinIO, Cloudflare R2) and attachments are visible team-wide with no export
  round-trip. Needs the `s3` extra (`uv pip install 'kantaq-core[s3]'`).

```
# in the runtime's .env (the machine running `kantaq`)
KANTAQ_BLOB_STORE=s3
KANTAQ_S3_BUCKET=kantaq-attachments
KANTAQ_S3_ENDPOINT_URL=https://<accountid>.r2.cloudflarestorage.com   # omit for AWS
KANTAQ_S3_REGION=auto
KANTAQ_S3_ACCESS_KEY_ID=...            # or rely on boto3's default provider chain
KANTAQ_S3_SECRET_ACCESS_KEY=...
```

Both stores are content-addressed by SHA-256 and re-verify bytes against their
address on read, so a ref is always re-checkable and the same file is stored
once. (The sync-server proxying blobs + audit-range reads remain deferred —
DEBT-39.)

## Backup & restore

Two complementary backstops (E25-T3 — guidance, not automated DR):

1. **Operational (volume snapshot).** Postgres data is in the `kantaq-pg-data`
   volume; attachment bytes are in your blob store (the filesystem dir or the S3
   bucket). A periodic logical dump is the simplest restore point:

   ```bash
   # back up (cron this)
   docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
     | gzip > kantaq-$(date +%F).sql.gz
   # restore into a fresh stack
   gunzip -c kantaq-2026-06-21.sql.gz \
     | docker compose exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"
   ```

   For S3 attachments, your object store's own versioning/replication is the
   blob backstop; for the filesystem store, snapshot the runtime's `blobs/` dir.

2. **Portable (the export bundle, MOD-23) — the data-sovereignty backstop.**
   `kantaq export` writes a signed, deterministic, self-verifying tarball
   (event logs + snapshots + blobs + the trust roots). Re-importing it into a
   fresh runtime replays the signed history and re-content-addresses every blob,
   so the team can rebuild on any backend — the cross-replica + leave-any-backend
   guarantee. The **restore-from-backup smoke** (`test_import_roundtrip.py`)
   pins it: a bundle restores into a fresh runtime (filesystem **or** S3 blob
   store) and re-exports byte-identically.

## Operations

- **Logs**: `docker compose logs -f sync-server`.
- **Upgrade**: `git pull`, then `docker compose up -d --build`.
- **Reset** (destroys data): `docker compose down -v`.

## TLS & secret hygiene (hardening, E25-T4)

- **HTTPS** is the documented default for any non-localhost deployment: set
  `CADDY_DOMAIN` to a public hostname and run `--profile tls`. Caddy obtains and
  renews a Let's Encrypt certificate automatically (ACME) and adds HSTS +
  `nosniff` and strips its `Server` banner (see `Caddyfile`). Terminating TLS at
  your own upstream proxy instead is fine — point it at `sync-server:8889`.
- **Secrets** (`POSTGRES_PASSWORD`, member tokens, S3 keys) come from `.env` /
  the environment and are never logged: the server reads `KANTAQ_DATABASE_URL`
  but never echoes it, token verification caches a SHA-256 **digest** of the
  presented token (a heap dump yields nothing replayable), and auth failures
  return a bare 401 with no token in the body. Set a long random
  `POSTGRES_PASSWORD`; rotate member tokens via the normal grant/revocation flow.
- **Signature floor**: after your workspace cuts over to signed sync, set
  `KANTAQ_REQUIRE_SIGNATURE=true` on the sync-server — a client can only ratchet
  the requirement *stricter*, never relax it.
