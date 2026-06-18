-- supabase/deploy/v0.2_live_verify.sql
-- v0.2 live smoke — READ-ONLY schema verification for the DEBT-25/30 apply window.
--
-- Paste into the Supabase SQL Editor (maintainer's authenticated session) and Run.
-- Every row should read status = PASS. Pure catalog SELECTs — no writes, safe to
-- run against prod any number of times.
--
-- Covers the schema-side claims that CI proves only hermetically:
--   * DEBT-25  — events RPC is the sole commit path; the raw-insert door is shut.
--   * DEBT-30  — anchors / acks / retention present; the grant window is BIGINT;
--                and — the Supabase-Free foot-gun — compaction is actually
--                SCHEDULED, not merely installed.
-- The TIMED smokes (cross-replica revoke <5s, gateway <50ms) need the backend
-- running with the service-role key and are a SEPARATE maintainer step — see the
-- block at the bottom of this file.

-- ── Block 1: schema checks (run this whole block; expect every status = PASS) ──
with checks(ord, lane, chk, got, expect) as (
  -- DEBT-25: the events commit RPC is present and SECURITY DEFINER
  select 1, 'DEBT-25', 'events RPC is SECURITY DEFINER',
         coalesce((select bool_or(p.prosecdef)::text
                     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                    where p.proname = 'events'), '(missing)'), 'true'
  union all
  -- DEBT-25: raw-push door shut — authenticated may SELECT but NOT INSERT
  select 2, 'DEBT-25', 'sync_events: authenticated INSERT revoked',
         has_table_privilege('authenticated','public.sync_events','INSERT')::text, 'false'
  union all
  select 3, 'DEBT-25', 'sync_events: authenticated SELECT kept',
         has_table_privilege('authenticated','public.sync_events','SELECT')::text, 'true'
  union all
  -- append-only: a non-internal BEFORE UPDATE/DELETE trigger guards committed history
  select 4, 'append-only', 'sync_events immutability trigger present',
         (exists(select 1 from pg_trigger t join pg_class c on c.oid = t.tgrelid
                  where c.relname = 'sync_events' and not t.tgisinternal))::text, 'true'
  union all
  -- conflict_records catch-up: table / RLS / policy / allowlist
  select 5, 'conflict_records', 'table exists',
         (to_regclass('public.conflict_records') is not null)::text, 'true'
  union all
  select 6, 'conflict_records', 'RLS enabled',
         coalesce((select c.relrowsecurity::text from pg_class c
                     join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname='public' and c.relname='conflict_records'), 'false'), 'true'
  union all
  select 7, 'conflict_records', 'select policy present',
         (exists(select 1 from pg_policies
                  where tablename='conflict_records' and policyname='conflict_records_select'))::text, 'true'
  union all
  select 8, 'conflict_records', 'in sync allowlist',
         coalesce((pg_get_constraintdef((select oid from pg_constraint
                    where conname='ck_sync_events_collection')) like '%conflict_records%')::text, 'false'), 'true'
  union all
  -- DEBT-30: E06 sync_acks present + RLS
  select 9, 'DEBT-30', 'sync_acks table + RLS',
         coalesce((select c.relrowsecurity::text from pg_class c
                     join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname='public' and c.relname='sync_acks'), 'false'), 'true'
  union all
  -- DEBT-30: E07 audit_anchors present + RLS
  select 10, 'DEBT-30', 'audit_anchors table + RLS',
         coalesce((select c.relrowsecurity::text from pg_class c
                     join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname='public' and c.relname='audit_anchors'), 'false'), 'true'
  union all
  -- DEBT-26: the capability grant window widened to BIGINT
  select 11, 'DEBT-26', 'capability_grants.expires_at is bigint',
         coalesce((select format_type(a.atttypid, a.atttypmod) from pg_attribute a
                    where a.attrelid = (select c.oid from pg_class c
                       join pg_namespace n on n.oid=c.relnamespace
                      where n.nspname='public' and c.relname='capability_grants')
                      and a.attname='expires_at'), '(missing)'), 'bigint'
  union all
  -- DEBT-30: retention function installed
  select 12, 'DEBT-30', 'kantaq.compact_sync_events installed',
         (exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                  where n.nspname='kantaq' and p.proname='compact_sync_events'))::text, 'true'
  union all
  -- DEBT-30: pg_cron extension present (the schedule itself is checked in Block 2)
  select 13, 'DEBT-30', 'pg_cron extension installed',
         (exists(select 1 from pg_extension where extname='pg_cron'))::text, 'true'
)
select ord as "#", lane, chk as check, expect, got,
       case when got = expect then 'PASS' else '>>> FAIL <<<' end as status
from checks order by ord;

-- ── Block 1b: full table + function parity (run this block) ──────────────────
-- Spot-checks above can miss a table that simply never got applied. This asserts
-- the WHOLE expected surface (19 tables + the RLS-helper / commit functions) so
-- live drift can't hide. Expect no '>>> MISSING <<<' and no '>>> RLS OFF <<<'.
with expected(kind, t) as (values
  ('collection','workspaces'),('collection','projects'),('collection','tickets'),
  ('collection','comments'),('collection','ticket_relationships'),('collection','members'),
  ('collection','tokens'),('collection','devices'),('collection','capability_grants'),
  ('collection','agent_proposals'),('collection','memory_entries'),('collection','memory_links'),
  ('collection','skill_containers'),('collection','skill_mappings'),('collection','conflict_records'),
  ('collection','audit_anchors'),('collection','audit_events'),
  ('infra','sync_events'),('infra','sync_acks')
)
select e.kind, e.t as expected_table,
  case when c.oid is null then '>>> MISSING <<<' else 'present' end as exists,
  case when c.oid is null then '-' when c.relrowsecurity then 'on' else '>>> RLS OFF <<<' end as rls
from expected e
left join pg_class c on c.relname = e.t and c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
order by e.kind, e.t;

select obj as expected_function,
  case when exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                    where n.nspname||'.'||p.proname = obj) then 'present' else '>>> MISSING <<<' end as exists
from (values ('kantaq.is_member'),('kantaq.member_ids'),('kantaq.actor_in_my_workspaces'),
             ('kantaq.compact_sync_events'),('public.events')) f(obj)
order by obj;

-- ── Block 2: the pg_cron schedule (run SEPARATELY) ───────────────────────────
-- cron.job lives in the `cron` schema and only exists once pg_cron is enabled.
-- On Supabase Free, compact_sync_events can be INSTALLED yet never SCHEDULED —
-- this is the check that catches it. Expect >= 1 active row.
--   select jobname, schedule, active, command
--     from cron.job
--    where command ilike '%compact_sync_events%';
-- ZERO rows  => compaction will NEVER run => the retention NFR is false in prod.

-- ── Block 3: TIMED smokes (MAINTAINER — needs the backend + service-role key) ─
-- These cannot run from an anon-only client; run from the maintainer env where the
-- backend holds the service-role key, against the live project. Capture 3 numbers:
--   1. Cross-replica revocation latency: revoke a live grant, then poll a read
--      path on a replica until the gateway denies. Target < 5s wall-clock.
--   2. Gateway decision latency: P50/P95 of a gated call through the gateway.
--      Target < 50ms.
--   3. Retention execution: invoke kantaq.compact_sync_events(30) once (service-role),
--      confirm rows compacted == sync_compactable_below_rev, and that Block 2 shows
--      the daily pg_cron entry active.
-- Record the three results in the DEBT-30 line of debt.md, then ping for the tag cut.
