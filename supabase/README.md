# supabase/ — the team backend's SQL artifacts (MOD-05 / Epic E24)

What the maintainer applies to the team's Supabase project, in order:

| Order | File | What it is |
|---|---|---|
| 1 | `migrations/0001_collections.sql` | The 8 v0.0.5 collections, **generated** from the one SQLModel definition (`kantaq_db.models`, D-07). Do not edit; regenerate with `uv run python -m kantaq_backend_supabase.schema`. |
| 2 | `policies/0001_rls.sql` | Row Level Security: hand-written policies scoping every table by workspace and member, plus the `kantaq.*` helper functions. **Not optional** — a table without RLS is readable by any signed-in user. |
| 3 | `migrations/0002_sync_events.sql` | The shared sync event log (E24-T4): the table push commits into and pull reads from. **Hand-written** — backend infrastructure, not a collection mirror; its identity column is the D-05 commit order. |
| 4 | `policies/0002_sync_rls.sql` | RLS for the sync log: members read their workspaces' events and insert only as themselves; **append-only** — no client role can update or delete committed history. |

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
