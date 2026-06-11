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

(Equivalent: link the repo with the Supabase CLI and `supabase db push`.)

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

## v0.0.5 scope — what exists today

The runtime verifies connectivity against this project (auth health endpoint)
before serving. The Postgres schema, the magic-link auth client, and the Row
Level Security policies shipped with epic **E24** — the SQL you applied above
is tested in CI against a real Postgres with a tampered client that must fail
to read another workspace. Event sync follows in Sprint 2 (E04/E24-T4); until
then the runtime writes nothing to the project.
