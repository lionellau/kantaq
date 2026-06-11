-- supabase/policies/0001_rls.sql
-- Row Level Security for the kantaq collections (E24-T3, FR-E24-1, D-03).
-- Hand-written; apply AFTER supabase/migrations/0001_collections.sql.
--
-- The model (D-03, defense in depth): RLS is the COARSE layer — workspace and
-- member scope, enforced by Postgres itself even against a tampered client
-- that talks straight to the database with a valid user JWT. Capability
-- grants (v0.1) are the FINE layer and never widen what RLS allows.
--
-- Member linkage (v0.0.5): a signed-in Supabase Auth user maps to members
-- rows by the JWT's verified email claim (members are invited by email, E06;
-- the magic link proves ownership). An auth_user_id column is the v0.1
-- hardening. Revoked/invited members (status <> 'active') get nothing; a JWT
-- without an email claim links to nothing.
--
-- Roles (Supabase built-ins): `authenticated` carries every signed-in member;
-- `anon` gets NO grants at all — kantaq clients only use the anon key against
-- the Auth endpoints, never PostgREST; `service_role` stays backend-side
-- (BYPASSRLS) and never reaches a client (NFR-E24-1).
--
-- Identity attribution is part of the wall: rows that say who did something
-- (audit_events.actor_id, comments.author_actor_id, agent_proposals
-- .proposer_id) can only be INSERTed as yourself, and Owner-tier member rows
-- can only be written by an Owner (a Maintainer cannot lock out the Owner).

-- ---------------------------------------------------------------------------
-- Helpers. SECURITY DEFINER so policies on `members` can consult `members`
-- without RLS recursion; the definer (the migration-applying owner) bypasses
-- RLS inside the function body. They live in a dedicated `kantaq` schema so
-- PostgREST never exposes them as RPC, return only booleans or the caller's
-- own scope (no oracle on other tenants), and pin an empty search_path with
-- fully-qualified references (Supabase SECURITY DEFINER guidance).
-- ---------------------------------------------------------------------------

create schema if not exists kantaq;
grant usage on schema kantaq to authenticated, service_role;

-- The signed-in user's email from the verified JWT claim; NULL (matching
-- nothing, never '') when the claim is absent.
create or replace function kantaq.jwt_email()
returns text
language sql stable
set search_path = ''
as $$
  select nullif(lower(auth.jwt() ->> 'email'), '')
$$;

-- Member ids belonging to the signed-in user (active rows only).
create or replace function kantaq.member_ids()
returns setof varchar
language sql stable security definer
set search_path = ''
as $$
  select m.id from public.members m
  where m.status = 'active' and lower(m.email) = kantaq.jwt_email()
$$;

-- Workspaces the signed-in user is an active member of.
create or replace function kantaq.workspace_ids()
returns setof varchar
language sql stable security definer
set search_path = ''
as $$
  select m.workspace_id from public.members m
  where m.status = 'active' and lower(m.email) = kantaq.jwt_email()
$$;

create or replace function kantaq.is_member(ws varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select ws in (select kantaq.workspace_ids())
$$;

-- Owner/Maintainer manage members and tokens (mirrors the E06 role matrix).
create or replace function kantaq.is_admin(ws varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.members m
    where m.workspace_id = ws
      and m.status = 'active'
      and m.role in ('Owner', 'Maintainer')
      and lower(m.email) = kantaq.jwt_email()
  )
$$;

-- Owner-tier writes (touching an Owner row, minting an Owner) need an Owner.
create or replace function kantaq.is_owner(ws varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.members m
    where m.workspace_id = ws
      and m.status = 'active'
      and m.role = 'Owner'
      and lower(m.email) = kantaq.jwt_email()
  )
$$;

-- Bootstrap guard: a brand-new workspace has no members yet.
create or replace function kantaq.workspace_has_members(ws varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (select 1 from public.members m where m.workspace_id = ws)
$$;

-- Am I an admin of the workspace this member row belongs to?
create or replace function kantaq.is_admin_of_member(mid varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select kantaq.is_admin(
    (select m.workspace_id from public.members m where m.id = mid)
  )
$$;

-- Does this actor (a member id) belong to one of my workspaces?
create or replace function kantaq.actor_in_my_workspaces(mid varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select (select m.workspace_id from public.members m where m.id = mid)
         in (select kantaq.workspace_ids())
$$;

-- Is this ticket inside one of my workspaces? (ticket -> project -> workspace)
create or replace function kantaq.ticket_in_my_workspaces(tid varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.tickets t
    join public.projects p on p.id = t.project_id
    where t.id = tid and p.workspace_id in (select kantaq.workspace_ids())
  )
$$;

-- ---------------------------------------------------------------------------
-- Enable RLS on every collection and grant table access to the signed-in
-- role. With RLS enabled and no matching policy, the default is DENY — the
-- grants below are the ceiling, the policies are the gate. `anon` is granted
-- nothing on purpose. DELETE is granted only where v0.0.5 deletes are real
-- (tracker entities); members/tokens/audit_events keep their rows.
-- ---------------------------------------------------------------------------

grant usage on schema public to authenticated, service_role;

alter table workspaces      enable row level security;
alter table projects        enable row level security;
alter table tickets         enable row level security;
alter table comments        enable row level security;
alter table members         enable row level security;
alter table tokens          enable row level security;
alter table audit_events    enable row level security;
alter table agent_proposals enable row level security;

grant select, insert, update         on workspaces      to authenticated;
grant select, insert, update, delete on projects        to authenticated;
grant select, insert, update, delete on tickets         to authenticated;
grant select, insert, update, delete on comments        to authenticated;
grant select, insert, update         on members         to authenticated;
grant select, insert, update         on tokens          to authenticated;
grant select, insert                 on audit_events    to authenticated;
grant select, insert, update         on agent_proposals to authenticated;

grant all on all tables in schema public to service_role;

-- ---------------------------------------------------------------------------
-- workspaces — visible to members; updated by admins; any signed-in user may
-- create one (they become its Owner via the members bootstrap below).
-- ---------------------------------------------------------------------------

create policy workspaces_select on workspaces
  for select to authenticated
  using (kantaq.is_member(id));

create policy workspaces_insert on workspaces
  for insert to authenticated
  with check (true);

create policy workspaces_update on workspaces
  for update to authenticated
  using (kantaq.is_admin(id))
  with check (kantaq.is_admin(id));

-- no delete policy: workspaces are not deleted in v0.0.5.

-- ---------------------------------------------------------------------------
-- members — readable inside the workspace; managed by admins, but Owner rows
-- (existing or new) only by an Owner: a Maintainer can neither revoke nor
-- demote an Owner, nor mint one. The bootstrap arm lets the creator of a
-- brand-new workspace insert exactly one row: themselves, as active Owner.
-- No delete policy ever (status flips instead). The last-Owner guard stays
-- app-layer (E06); RLS guards the tier boundary.
-- ---------------------------------------------------------------------------

create policy members_select on members
  for select to authenticated
  using (kantaq.is_member(workspace_id));

create policy members_insert on members
  for insert to authenticated
  with check (
    (
      kantaq.is_admin(workspace_id)
      and (role <> 'Owner' or kantaq.is_owner(workspace_id))
    )
    or (
      not kantaq.workspace_has_members(workspace_id)
      and role = 'Owner'
      and status = 'active'
      and lower(email) = kantaq.jwt_email()
    )
  );

create policy members_update on members
  for update to authenticated
  using (
    kantaq.is_admin(workspace_id)
    and (role <> 'Owner' or kantaq.is_owner(workspace_id))
  )
  with check (
    kantaq.is_admin(workspace_id)
    and (role <> 'Owner' or kantaq.is_owner(workspace_id))
  );

-- ---------------------------------------------------------------------------
-- projects — workspace-coarse (D-03): any active member of the workspace.
-- ---------------------------------------------------------------------------

create policy projects_select on projects
  for select to authenticated
  using (kantaq.is_member(workspace_id));

create policy projects_insert on projects
  for insert to authenticated
  with check (kantaq.is_member(workspace_id));

create policy projects_update on projects
  for update to authenticated
  using (kantaq.is_member(workspace_id))
  with check (kantaq.is_member(workspace_id));

create policy projects_delete on projects
  for delete to authenticated
  using (kantaq.is_member(workspace_id));

-- ---------------------------------------------------------------------------
-- tickets — scoped through their project's workspace. UPDATE's WITH CHECK
-- re-validates project_id directly (not via the ticket's old project), so a
-- ticket cannot be moved into another workspace's project.
-- ---------------------------------------------------------------------------

create policy tickets_select on tickets
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(id));

create policy tickets_insert on tickets
  for insert to authenticated
  with check (
    exists (
      select 1 from projects p
      where p.id = project_id and kantaq.is_member(p.workspace_id)
    )
  );

create policy tickets_update on tickets
  for update to authenticated
  using (kantaq.ticket_in_my_workspaces(id))
  with check (
    exists (
      select 1 from projects p
      where p.id = project_id and kantaq.is_member(p.workspace_id)
    )
  );

create policy tickets_delete on tickets
  for delete to authenticated
  using (kantaq.ticket_in_my_workspaces(id));

-- ---------------------------------------------------------------------------
-- comments — readable across the workspace; written AS yourself (authorship
-- is attribution, like audit), and edited/deleted only by their author.
-- ---------------------------------------------------------------------------

create policy comments_select on comments
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id));

create policy comments_insert on comments
  for insert to authenticated
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and author_actor_id in (select kantaq.member_ids())
  );

create policy comments_update on comments
  for update to authenticated
  using (author_actor_id in (select kantaq.member_ids()))
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and author_actor_id in (select kantaq.member_ids())
  );

create policy comments_delete on comments
  for delete to authenticated
  using (author_actor_id in (select kantaq.member_ids()));

-- ---------------------------------------------------------------------------
-- agent_proposals — readable across the workspace; proposed AS yourself.
-- UPDATE stays workspace-coarse: approve/reject status flips are made by
-- members other than the proposer (the approval flow). proposer_id being
-- mutable post-insert by workspace members is accepted for v0.0.5 and closes
-- with v0.1 event signing (DEBT-01). Never client-deleted.
-- ---------------------------------------------------------------------------

create policy agent_proposals_select on agent_proposals
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id));

create policy agent_proposals_insert on agent_proposals
  for insert to authenticated
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and proposer_id in (select kantaq.member_ids())
  );

create policy agent_proposals_update on agent_proposals
  for update to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id))
  with check (kantaq.ticket_in_my_workspaces(ticket_id));

-- ---------------------------------------------------------------------------
-- tokens — by member: a member touches only their own token rows; workspace
-- admins manage everyone's (invite/revoke/rotate, E06). Hashes only ever sit
-- here (argon2id PHC); no delete policy — revoked rows are kept for audit.
-- ---------------------------------------------------------------------------

create policy tokens_select on tokens
  for select to authenticated
  using (
    member_id in (select kantaq.member_ids())
    or kantaq.is_admin_of_member(member_id)
  );

create policy tokens_insert on tokens
  for insert to authenticated
  with check (
    member_id in (select kantaq.member_ids())
    or kantaq.is_admin_of_member(member_id)
  );

create policy tokens_update on tokens
  for update to authenticated
  using (
    member_id in (select kantaq.member_ids())
    or kantaq.is_admin_of_member(member_id)
  )
  with check (
    member_id in (select kantaq.member_ids())
    or kantaq.is_admin_of_member(member_id)
  );

-- ---------------------------------------------------------------------------
-- audit_events — append-only AT THE DATABASE (E07's rule, server-enforced):
-- INSERT only as yourself, SELECT inside the actor's workspace, and no
-- UPDATE/DELETE policy for any client role — not even workspace Owners.
-- ---------------------------------------------------------------------------

create policy audit_events_select on audit_events
  for select to authenticated
  using (kantaq.actor_in_my_workspaces(actor_id));

create policy audit_events_insert on audit_events
  for insert to authenticated
  with check (actor_id in (select kantaq.member_ids()));
