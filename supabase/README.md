# supabase/ — the team backend's SQL artifacts (MOD-05 / Epic E24)

What the maintainer applies to the team's Supabase project, in order:

| Order | File | What it is |
|---|---|---|
| 1 | `migrations/0001_collections.sql` | The 8 v0.0.5 collections, **generated** from the one SQLModel definition (`kantaq_db.models`, D-07). Do not edit; regenerate with `uv run python -m kantaq_backend_supabase.schema`. |
| 2 | `policies/0001_rls.sql` | Row Level Security: hand-written policies scoping every table by workspace and member, plus the `kantaq.*` helper functions. **Not optional** — a table without RLS is readable by any signed-in user. |
| 3 | `migrations/0002_sync_events.sql` | The shared sync event log (E24-T4): the table push commits into and pull reads from. **Hand-written** — backend infrastructure, not a collection mirror; its identity column is the D-05 commit order. The collection allowlist now includes `devices`/`capability_grants` (E24-T7). |
| 4 | `policies/0002_sync_rls.sql` | RLS for the sync log: members read their workspaces' events and insert only as themselves; **append-only** — no client role can update or delete committed history. |
| 5 | `rpc/events.sql` | The v0.2 atomic commit RPC (E24-T6, D-09): validates grant + ordering, applies the merge policy, assigns the revision, and commits in one transaction — closing the commit-visibility window. **Hand-written.** Apply after the two `0002` files. |
| 6 | `policies/0003_append_only.sql` | The append-only trigger (E24-T7): makes committed history immutable **even for `service_role`** (a `BEFORE UPDATE OR DELETE` trigger fires past BYPASSRLS). Apply last. |

## Applying (E24-T0 is manual — a person does this once)

1. Create the Supabase project (see [docs/setup-supabase.md](../docs/setup-supabase.md)).
2. Open the project's **SQL Editor**, paste and run the four files above **in
   order** (the policies define helpers the later files build on). Or use the
   Supabase CLI: `supabase db push` with these files in your linked project's
   `supabase/migrations/`.
3. In **Authentication → Sign In / Up**, leave **Email** enabled (magic links are
   the v0.0.5 sign-in; kantaq requests them invite-only with `create_user=false`).
4. Share the **Project URL** and **anon key** with the team (`.env.supabase.example`).
   The **service-role key stays in the dashboard** — no kantaq client ever reads
   it, and the policies are written assuming it never leaves the backend (NFR-E24-1).

## Schema updates for existing projects

Projects created **before E13 memory sync / E12 relations** (sprint-3) or
**before the v0.2 trust-root ingest** (E24-T7) carry an older allowlist on
`sync_events` and will refuse the newer collections' events (breaking the whole
push batch). Run once in the SQL Editor to bring the constraint up to the
current set (this block is pinned byte-for-collection against the checked-in
`ck_sync_events_collection` by `tests/test_sync_allowlists.py`):

```sql
ALTER TABLE sync_events DROP CONSTRAINT ck_sync_events_collection;
ALTER TABLE sync_events ADD CONSTRAINT ck_sync_events_collection CHECK (collection IN
  ('workspaces', 'projects', 'tickets', 'comments', 'ticket_relationships',
   'members', 'agent_proposals', 'memory_entries', 'memory_links',
   'devices', 'capability_grants'));
```

Then apply the v0.2 backend additions (idempotent — `create or replace`):

```sql
-- the atomic commit RPC and the append-only trigger
\i rpc/events.sql
\i policies/0003_append_only.sql
```

> **Live-drift note (schema-SOP gates 8–9).** A project still on the original
> v0.0.5 8-collection set must first run the additive `0001_collections.sql` +
> `0001_rls.sql` so `devices`/`capability_grants` (and the other v0.1 tables)
> exist before the trust roots can ingest. The agent **authors** this catch-up
> SQL by copying the checked-in artifacts verbatim; a **human applies it** to
> the live project and re-verifies `list_tables` == `COLLECTION_MODELS`. No
> agent writes to the live shared backend.

(New projects just apply files 1–6 above in order, which already include all of
this.)

## How this is tested

CI applies these exact files to a disposable Postgres 16 with a faithful stub
of Supabase's auth environment (`kantaq_test_harness.rls`), then attacks them
with a tampered client — direct SQL under the `authenticated` role with forged
JWT claims. Cross-workspace reads and writes must come back empty or denied
(`adapters/backend-supabase/tests/test_rls.py`, `test_sync_rls.py`). The sync
adapter itself is driven through `FakePostgREST` — the same REST dialect
Supabase serves, answered by real SQL with the claims and role applied — so
push/pull/LWW are proven against real RLS (`test_sync_live.py`). A drift gate
keeps `0001_collections.sql` byte-identical to what the models generate.
