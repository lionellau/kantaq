# Setting up the shared Supabase backend (team mode)

One person — the team's **maintainer** — does this once. Everyone else just
receives two values (project URL + anon key) and follows the team-mode steps in
the [QUICKSTART](../QUICKSTART.md).

kantaq uses Supabase as the team sync backend: a Postgres database (with Row
Level Security), email/magic-link auth, and file storage. It stores committed
events only. It never runs application code and nobody's agent ever talks to
it directly — agents stay on each member's loopback gateway.

## 1. Create the project

1. Sign in at [supabase.com](https://supabase.com) (the **Free tier is enough**
   for a 2–10 person team).
2. **New project** → pick your organization, name it (e.g. `kantaq-yourteam`),
   choose a region close to the team, and let Supabase generate the database
   password. Store that password in your password manager — kantaq members
   never need it, but you will for database administration.

## 2. Apply the schema and the security policies

In the project dashboard open the **SQL Editor** and run, in order:

1. [`supabase/migrations/0001_collections.sql`](../supabase/migrations/0001_collections.sql)
   — the 8 kantaq collections (generated from the same model definition the
   local replica uses, so the two stores cannot drift).
2. [`supabase/policies/0001_rls.sql`](../supabase/policies/0001_rls.sql) —
   Row Level Security. **Do not skip this**: without it any signed-in member
   could read every workspace. With it, Postgres itself scopes every read and
   write by workspace and member — even against a tampered client.
3. [`supabase/migrations/0002_sync_events.sql`](../supabase/migrations/0002_sync_events.sql)
   — the shared sync event log members push to and pull from (E24-T4).
4. [`supabase/policies/0002_sync_rls.sql`](../supabase/policies/0002_sync_rls.sql)
   — RLS for the log: workspace-scoped, self-attributed, append-only.

(Equivalent: link the repo with the Supabase CLI and `supabase db push`.)

Already ran the first two files during Sprint 1? Just run files 3 and 4 — every
policy file re-applies cleanly as-is.

Magic-link sign-in needs no extra setup — **Email** auth is enabled by default,
and kantaq requests links invite-only (no self-signup accounts).

## 3. Capture the two values members need

In the project dashboard under **Settings → API**:

- **Project URL** — `https://<your-project-ref>.supabase.co`
- **anon (public) key** — the publishable key clients use

Share exactly these two values with your team through a password manager or
another channel you trust.

## 4. What stays secret

The same settings page also shows a **service_role key**. It bypasses Row
Level Security.

- **Never** put it in a member's `.env` — the runtime neither reads nor needs it.
- **Never** commit it anywhere. kantaq's `.env` files are gitignored; only
  `*.example` files are tracked.
- If it ever leaks, rotate it from that same dashboard page.

This is a load-bearing security rule (NFR-E06-1, NFR-E24-1): the service-role
key never leaves the backend side. CI tests assert no secret material appears
in any client-facing response, and the kantaq Supabase client refuses outright
to be constructed with a service-role key.

## 5. Point members at it

Each member copies the example env and fills in the two shared values:

```bash
cp .env.supabase.example .env
# edit .env:
#   SUPABASE_URL=https://<your-project-ref>.supabase.co
#   SUPABASE_ANON_KEY=<the anon key you shared>
kantaq doctor
```

`kantaq doctor` (and every `kantaq dev`) checks the backend is reachable and
fails fast with a clear message if not.

## 6. Operational notes

- **Free-tier pause.** Supabase pauses free projects after about 7 days of
  inactivity. Restoring takes one click in the dashboard; members see a
  `connection verify failed` until you do.
- **Region matters once sync lands** — pick the region where most of the team
  works; you cannot cheaply move it later.
- **Cost ceiling.** A 2–10 person team fits comfortably in the Free tier; the
  first paid tier is the escape hatch, not a requirement.

## 7. The team manifest (v0.0.5 — until the onboarding UI lands)

Sync is scoped by Row Level Security, and RLS decides who someone is by their
row in the **members** table — so the maintainer seeds one workspace row and
one member row per teammate before first sync. In the SQL Editor:

```sql
-- The shared workspace. Use the maintainer's LOCAL workspace id so the
-- maintainer's own backlog (already in their event log) can push:
-- find it with:  select id from workspaces;  on your local replica.
insert into workspaces (id, created_at, updated_at, actor_seq, visibility,
  hosting_mode, retention_policy, name)
values ('<workspace-ulid>', now(), now(), 0, 'team', 'plain', 'standard',
        'Your Team');

-- One row per member. For the maintainer, reuse their LOCAL member id
-- (select id from members; on the local replica) with their REAL sign-in
-- email; teammates get fresh ULIDs.
insert into members (id, created_at, updated_at, actor_seq, visibility,
  hosting_mode, retention_policy, workspace_id, email, role, status)
values ('<member-ulid>', now(), now(), 0, 'team', 'plain', 'standard',
        '<workspace-ulid>', 'person@team.dev', 'Member', 'active');
```

Each teammate also needs a Supabase Auth user for the magic link: dashboard →
**Authentication → Users → Invite user** (kantaq itself requests sign-in links
invite-only and never creates accounts).

The E21 onboarding UI automates this; the manifest is the documented v0.0.5
path.

## 8. Syncing (E24-T4)

Each member, after `kantaq doctor` passes:

```bash
kantaq sync login --email person@team.dev   # emailed one-time code → keychain
kantaq sync once                            # one push + pull cycle
kantaq sync status                          # local pending/cursor state
```

The acting member is resolved from the members table by the session's verified
email; events commit in the backend's order (last writer wins), re-pushing is
idempotent, and the log is append-only — Postgres refuses every client UPDATE
or DELETE on it, even from a workspace Owner.

## v0.0.5 scope — what exists today

The runtime verifies connectivity against this project (auth health endpoint)
before serving. The Postgres schema, the magic-link auth client, and the Row
Level Security policies shipped with Sprint 1 of epic **E24**; the sync
endpoints (the `sync_events` log + push/pull over Supabase's data API) shipped
with **E24-T4**. All of it is tested in CI against a real Postgres with a
tampered client that must fail to read or write another workspace. Known
v0.0.5 limits: online-only (offline outbox is v0.2), unsigned events (Ed25519
verification is v0.1), and one workspace per member.
