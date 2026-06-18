# adapters/

Sync-backend adapters. Each implements the same validate-store-and-assign-revision
contract, so kantaq's team backend can be swapped without touching the rest of the stack.

- **`backend-supabase/`** — the Supabase adapter, what a team syncs through today. It
  validates and stores committed events, and Row Level Security keeps every workspace's
  data scoped to its own members. Setup is one-time and maintainer-only:
  [docs/setup-supabase.md](../docs/setup-supabase.md).
- **`backend-postgres/`** — a self-hosted Postgres adapter for teams that would rather
  run their own backend on a small VPS. Coming in v0.3.
