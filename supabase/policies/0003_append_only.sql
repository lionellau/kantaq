-- supabase/policies/0003_append_only.sql
-- HAND-WRITTEN (E24-T7, FR-E24-3, exit criterion 3): committed history is
-- append-only at the GRANT LAYER — even for service_role. Apply AFTER
-- policies/0002_sync_rls.sql. Re-appliable as-is.
--
-- WHY A TRIGGER, NOT JUST GRANTS. 0002_sync_rls.sql withholds UPDATE/DELETE
-- from every CLIENT role, so authenticated/anon already cannot rewrite the log.
-- But service_role carries BYPASSRLS and holds `grant all`, so it COULD UPDATE
-- or DELETE a committed row, and an INSERT .. ON CONFLICT DO UPDATE would slip
-- past a missing-grant rule. A BEFORE trigger fires for EVERY role, BYPASSRLS
-- included, so it is the only mechanism that makes the log truly immutable
-- below the app layer (the audit hash chain, E07, already makes any tampering
-- evident; this makes it impossible through the ordinary write paths).
--
-- The atomic commit RPC (supabase/rpc/events.sql) only ever INSERTs .. ON
-- CONFLICT DO NOTHING, so it is unaffected. The Sprint-7 retention compaction
-- (MOD-05 / MOD-27) deletes below a safe ack watermark; it will introduce a
-- guarded bypass (a service-role pg_cron job setting a session GUC these
-- triggers check) at that time — deliberately NOT added here so v0.2 keeps the
-- strict immutability guarantee.
--
-- TWO triggers are needed: a row-level one for UPDATE/DELETE and a
-- statement-level one for TRUNCATE (TRUNCATE fires neither a row trigger nor an
-- UPDATE/DELETE trigger, and service_role holds `grant all` incl. TRUNCATE +
-- BYPASSRLS — so without this second trigger it could wipe the whole log,
-- defeating the very guarantee this file makes).

-- One function serves both triggers below. It references neither OLD nor NEW so
-- it is valid for the statement-level TRUNCATE trigger (where OLD is null) as
-- well as the row-level UPDATE/DELETE trigger; TG_OP names the blocked write.
create or replace function kantaq.sync_events_no_rewrite()
returns trigger
language plpgsql
as $$
begin
  raise exception
    'sync_events is append-only: % is not permitted on committed history',
    tg_op
    using errcode = '42501';  -- insufficient_privilege
end;
$$;

drop trigger if exists sync_events_append_only on sync_events;
create trigger sync_events_append_only
  before update or delete on sync_events
  for each row execute function kantaq.sync_events_no_rewrite();

-- TRUNCATE is a statement-level DDL write that escapes the row trigger above;
-- this closes that hole so even service_role cannot erase committed history.
drop trigger if exists sync_events_no_truncate on sync_events;
create trigger sync_events_no_truncate
  before truncate on sync_events
  for each statement execute function kantaq.sync_events_no_rewrite();
