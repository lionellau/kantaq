-- supabase/policies/0002_sync_rls.sql
-- Row Level Security for the shared sync event log (E24-T4, FR-E24-1, D-03).
-- Hand-written; apply AFTER supabase/migrations/0002_sync_events.sql (and
-- after policies/0001_rls.sql, which defines the kantaq.* helpers this file
-- builds on). Re-appliable as-is, like 0001.
--
-- The model mirrors audit_events: the log is APPEND-ONLY at the database.
-- Members read the events of their own workspaces and insert events only as
-- themselves (actor attribution is part of the wall — a member cannot forge
-- another member's events, the same F3 rule as comments/proposals). No
-- UPDATE/DELETE policy *or grant* exists for any client role: committed
-- history cannot be rewritten, which is what makes "LWW by commit order"
-- trustworthy. v0.0.5 events are unsigned (DEBT-01); Ed25519 + grant
-- verification arrive v0.1 (FR-E24-2).

-- ---------------------------------------------------------------------------
-- Helper: is this actor (a member id) the signed-in user themselves, active,
-- and a member of this workspace? SECURITY DEFINER + empty search_path +
-- boolean-only oracle, per the 0001 conventions.
-- ---------------------------------------------------------------------------

create or replace function kantaq.is_self_in_workspace(mid varchar, ws varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.members m
    where m.id = mid
      and m.workspace_id = ws
      and m.status = 'active'
      and lower(m.email) = kantaq.jwt_email()
  )
$$;

-- ---------------------------------------------------------------------------
-- Grants: strip the Supabase auto-grant back to the ceiling (the 0001 block
-- already trimmed anon's default privileges; authenticated still arrives
-- over-granted on new tables), then grant exactly select + insert. No client
-- role ever holds UPDATE or DELETE here.
-- ---------------------------------------------------------------------------

revoke all on sync_events from anon, authenticated;

alter table sync_events enable row level security;

grant select, insert on sync_events to authenticated;
grant all on sync_events to service_role;

-- ---------------------------------------------------------------------------
-- sync_events — readable inside your workspaces; INSERTed only as yourself
-- into a workspace you are an active member of. Append-only: no UPDATE or
-- DELETE policy for any client role, not even workspace Owners.
-- ---------------------------------------------------------------------------

drop policy if exists sync_events_select on sync_events;
create policy sync_events_select on sync_events
  for select to authenticated
  using (kantaq.is_member(workspace_id));

drop policy if exists sync_events_insert on sync_events;
create policy sync_events_insert on sync_events
  for insert to authenticated
  with check (kantaq.is_self_in_workspace(actor_id, workspace_id));
