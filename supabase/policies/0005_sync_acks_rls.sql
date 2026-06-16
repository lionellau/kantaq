-- supabase/policies/0005_sync_acks_rls.sql
-- HAND-WRITTEN (E07-T4 / FR-E26-2, MOD-05). RLS for the ack-watermark table.
-- Apply AFTER migrations/0003_sync_acks.sql. Re-appliable as-is.
--
-- A member reports their OWN replica's acked revision in a workspace they
-- actively belong to. TWO scoping guards (E07-T4 SEC fix — the cross-member
-- stranding hole): kantaq.is_member(workspace_id) bars another workspace, and
-- member_id in kantaq.member_ids() bars writing a row attributed to a *peer*.
-- A member can therefore only create or move rows under their OWN member_id, so
-- they can never raise the MIN-acked watermark to delete a row a peer still
-- needs: their own rows can only LOWER the min (under-report → compaction holds,
-- fail-safe) or not affect it, and a peer's real ack always records under the
-- peer's own (member_id, replica_id) key (member_id is in the PK). No DELETE
-- grant: ack rows are upserted, never removed by a client.

alter table sync_acks enable row level security;

-- Strip the Supabase auto-grant (new public tables get ALL to anon/authenticated)
-- back to the ceiling, then grant exactly select/insert/update — no client DELETE
-- (ack rows are upserted, never removed by a client), no anon access at all.
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
