# adapters/

Sync-backend adapters. Each implements the same validate/store/assign-revision
contract behind the protocol objects, so the backend can be swapped without
touching the rest of the stack.

- `backend-supabase/` — Supabase adapter (MOD-05, Epic E24). Lands in Sprint 1–2.
- `backend-postgres/` — self-hosted Postgres adapter (MOD-28, Epic E25). v0.3.

Scaffolded empty in Epic E01; populated by their owning epics.
