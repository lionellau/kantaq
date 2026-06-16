-- supabase/rpc/retention.sql
-- HAND-WRITTEN (E07-T4 / FR-E26-2, MOD-05 + MOD-27): the watermark-safe
-- sync_events compaction (the larger v0.2 cost lever). Apply AFTER
-- migrations/0003_sync_acks.sql and policies/0003_append_only.sql (the GUC
-- bypass the function relies on). Re-appliable as-is.
--
-- kantaq.compact_sync_events(ttl_days) deletes, per workspace, only rows that
-- are BOTH below the safe ack watermark AND older than the TTL:
--
--   safe_watermark = MIN(acked_rev) over replicas that acked within ttl_days
--   DELETE FROM sync_events WHERE revision < safe_watermark AND committed_at < cutoff
--
-- A replica silent past ttl_days is excluded from the watermark (it is treated
-- as stale and re-snapshots, MOD-26 snapshot-then-stream) rather than holding
-- the prune back forever; a replica still live but lagging is NEVER stranded —
-- nothing at or above its acked_rev is deleted. The function sets the
-- transaction-local GUC the append-only trigger checks (policies/0003), so the
-- DELETE is the one sanctioned below-app-layer path; it never UPDATEs or
-- TRUNCATEs. Returns the rows compacted (observability). The runtime's
-- core.retention.run reports `sync_compactable_below_rev`; THIS is the execution.

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
  -- Operator foot-gun guard: a misconfigured pg_cron arg should fail loudly, not
  -- silently disable compaction (ttl_days=0 → cutoff=now → nothing ever matches)
  -- or prune an absurd window.
  if ttl_days < 1 or ttl_days > 365 then
    raise exception 'compact_sync_events: ttl_days must be in [1, 365], got %', ttl_days;
  end if;
  -- The sanctioned, transaction-scoped retention bypass (policies/0003).
  set local kantaq.retention_compaction = 'on';
  for w in select id from public.workspaces loop
    -- The lowest revision EVERY live replica has acked; a replica silent past
    -- the TTL is excluded (it re-snapshots). NULL when no live replica has acked
    -- → nothing is safe to delete for this workspace, so skip it.
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

-- The compaction is service-role-only: it both holds the DELETE grant and runs
-- under pg_cron. No client role may invoke it.
revoke all on function kantaq.compact_sync_events(integer) from public;
revoke all on function kantaq.compact_sync_events(integer) from anon;
revoke all on function kantaq.compact_sync_events(integer) from authenticated;

-- Schedule the daily compaction via pg_cron (a Supabase PLATFORM extension, not
-- a vendored library — the golden-rule star bar does not apply). GUARDED: pg_cron
-- is absent from stock Postgres (CI / self-host), so where it is installed this
-- schedules the job and where it is absent it is a no-op. CI proves the
-- compaction FUNCTION hermetically; this block is its cadence on live Supabase
-- (a maintainer apply step). Re-running cron.schedule with the same name updates
-- the schedule in place.
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
