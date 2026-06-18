-- supabase/deploy/v0.2_debt30_catchup.sql
-- v0.2 live catch-up for the DEBT-30 audit/retention/identity batch — the deploy
-- authored to the repo (E07-T4/T5, DEBT-26) but never applied to the live shared
-- project. Verified missing by supabase/deploy/v0.2_live_verify.sql on 2026-06-18:
--   * audit_anchors                 -> table + RLS MISSING
--   * sync_acks                     -> table + RLS MISSING
--   * kantaq.compact_sync_events    -> function MISSING (so the pg_cron schedule
--                                      was a silent no-op even after pg_cron was on)
--   * capability_grants.issued_at / expires_at -> still INTEGER, need BIGINT (DEBT-26)
-- Everything else (17 tables incl. audit_events, all RLS on; is_member / member_ids /
-- actor_in_my_workspaces / public.events) is already live.
--
-- SOURCE: every statement copied verbatim from the checked-in artifacts (schema-SOP
-- — the agent authors, a human applies):
--   * sync_acks table       -> migrations/0003_sync_acks.sql
--   * sync_acks RLS         -> policies/0005_sync_acks_rls.sql
--   * audit_anchors table   -> migrations/0001_collections.sql
--   * audit_anchors RLS     -> policies/0001_rls.sql (enable + ceiling + append-only policies)
--   * BIGINT window         -> migrations/0001_collections.sql (DEBT-26 widening)
--   * retention fn + cron   -> rpc/retention.sql
--
-- SAFE + ADDITIVE: two new backend-infra tables (off the sync allowlist, like
-- sync_events), a lossless integer->bigint widening, and a service-role-only
-- function. No client write path changes — nothing here can break a running client.
-- IDEMPOTENT: re-runnable (IF NOT EXISTS / create or replace / drop policy if exists);
-- safe to re-apply if any step is interrupted.
--
-- APPLY: paste into the Supabase SQL Editor and Run (a human applies this; no agent
-- writes to the live backend). pg_cron must already be enabled (it is). Then run
-- supabase/deploy/v0.2_live_verify.sql — every row should read PASS.

-- 0. Preconditions — the RLS helpers these policies bind to must already be live
--    (fail loud BEFORE any DDL rather than mid-script).
do $$
begin
  if not exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'kantaq' and p.proname = 'actor_in_my_workspaces') then
    raise exception 'PRECONDITION: kantaq.actor_in_my_workspaces() missing — apply policies/0001_rls.sql helpers first';
  end if;
  if not exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'kantaq' and p.proname = 'member_ids') then
    raise exception 'PRECONDITION: kantaq.member_ids() missing — apply policies/0001_rls.sql helpers first';
  end if;
end $$;

-- 1. sync_acks — the ack-watermark table (migrations/0003_sync_acks.sql).
CREATE TABLE IF NOT EXISTS sync_acks (
	workspace_id VARCHAR(26) NOT NULL,
	member_id VARCHAR(26) NOT NULL,
	replica_id VARCHAR(26) NOT NULL,
	acked_rev BIGINT NOT NULL DEFAULT 0,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
	PRIMARY KEY (workspace_id, member_id, replica_id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);
CREATE INDEX IF NOT EXISTS ix_sync_acks_workspace ON sync_acks (workspace_id, acked_rev);

-- 1b. sync_acks RLS (policies/0005_sync_acks_rls.sql).
alter table sync_acks enable row level security;
revoke all on sync_acks from anon, authenticated;
grant select, insert, update on sync_acks to authenticated;
grant all on sync_acks to service_role;

drop policy if exists sync_acks_select on sync_acks;
create policy sync_acks_select on sync_acks
  for select to authenticated
  using (kantaq.is_member(workspace_id));

drop policy if exists sync_acks_insert on sync_acks;
create policy sync_acks_insert on sync_acks
  for insert to authenticated
  with check (
    kantaq.is_member(workspace_id)
    and member_id in (select kantaq.member_ids())
  );

drop policy if exists sync_acks_update on sync_acks;
create policy sync_acks_update on sync_acks
  for update to authenticated
  using (
    kantaq.is_member(workspace_id)
    and member_id in (select kantaq.member_ids())
  )
  with check (
    kantaq.is_member(workspace_id)
    and member_id in (select kantaq.member_ids())
  );

-- 2. audit_anchors — Merkle anchor table + indexes (migrations/0001_collections.sql).
CREATE TABLE IF NOT EXISTS audit_anchors (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	actor_id VARCHAR NOT NULL,
	range_start VARCHAR(26) NOT NULL,
	range_end VARCHAR(26) NOT NULL,
	merkle_root VARCHAR(64) NOT NULL,
	tree_size INTEGER NOT NULL,
	chain_tip VARCHAR(64) NOT NULL,
	external_pin VARCHAR,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_audit_anchors_actor_id ON audit_anchors (actor_id);
CREATE INDEX IF NOT EXISTS ix_audit_anchors_range_end ON audit_anchors (range_end);
CREATE INDEX IF NOT EXISTS ix_audit_anchors_range_start ON audit_anchors (range_start);

-- 2b. audit_anchors RLS (policies/0001_rls.sql). The explicit revoke replicates the
--     global ceiling (0001_rls line 168) for a table created AFTER that block ran —
--     Supabase auto-grants ALL to anon/authenticated on every new public table.
alter table audit_anchors enable row level security;
revoke all on audit_anchors from anon, authenticated;
grant select, insert on audit_anchors to authenticated;
grant all on audit_anchors to service_role;

drop policy if exists audit_anchors_select on audit_anchors;
create policy audit_anchors_select on audit_anchors
  for select to authenticated
  using (kantaq.actor_in_my_workspaces(actor_id));

drop policy if exists audit_anchors_insert on audit_anchors;
create policy audit_anchors_insert on audit_anchors
  for insert to authenticated
  with check (actor_id in (select kantaq.member_ids()));

-- 3. capability_grants window -> BIGINT (DEBT-26, schema v15). Lossless widening of
--    the existing integer columns to match migrations/0001_collections.sql.
alter table capability_grants
  alter column issued_at  type bigint,
  alter column expires_at type bigint;

-- 4. Retention: the watermark-safe sync_events compaction (rpc/retention.sql).
--    Depends on sync_acks (created above), public.workspaces, and the append-only
--    GUC trigger (policies/0003, already live).
create or replace function kantaq.compact_sync_events(ttl_days integer default 30)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cutoff timestamptz := now() - make_interval(days => ttl_days);
  v_deleted bigint := 0;
  v_n bigint;
  w record;
  v_watermark bigint;
begin
  if ttl_days < 1 or ttl_days > 365 then
    raise exception 'compact_sync_events: ttl_days must be in [1, 365], got %', ttl_days;
  end if;
  set local kantaq.retention_compaction = 'on';
  for w in select id from public.workspaces loop
    select min(acked_rev) into v_watermark
      from public.sync_acks
      where workspace_id = w.id and updated_at >= v_cutoff;
    if v_watermark is null then
      continue;
    end if;
    delete from public.sync_events
      where workspace_id = w.id
        and revision < v_watermark
        and committed_at < v_cutoff;
    get diagnostics v_n = row_count;
    v_deleted := v_deleted + v_n;
  end loop;
  return v_deleted;
end;
$$;

revoke all on function kantaq.compact_sync_events(integer) from public;
revoke all on function kantaq.compact_sync_events(integer) from anon;
revoke all on function kantaq.compact_sync_events(integer) from authenticated;

-- 4b. Schedule the daily compaction (rpc/retention.sql). pg_cron is enabled now, so
--     this actually creates the job (it was a silent no-op pre-enable). Re-running
--     with the same name updates the schedule in place.
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule(
      'kantaq-compact-sync-events',
      '17 3 * * *',  -- daily at 03:17 UTC, off-peak
      'select kantaq.compact_sync_events(30);'
    );
  end if;
end;
$$;

-- VERIFY: run supabase/deploy/v0.2_live_verify.sql (Block 1 should be all PASS, and
-- the parity block should show no MISSING), plus this for the schedule:
--   select jobname, schedule, active from cron.job where jobname = 'kantaq-compact-sync-events';
