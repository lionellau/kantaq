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

-- Is this project inside one of my workspaces? (E14 — milestones scope through
-- their project's workspace, exactly as tickets do.)
create or replace function kantaq.project_in_my_workspaces(pid varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.projects p
    where p.id = pid and p.workspace_id in (select kantaq.workspace_ids())
  )
$$;

-- Is this milestone inside one of my workspaces? (E14 — the ticket_milestones
-- junction gates its milestone endpoint through milestone -> project -> ws.)
create or replace function kantaq.milestone_in_my_workspaces(mid varchar)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.milestones m
    join public.projects p on p.id = m.project_id
    where m.id = mid and p.workspace_id in (select kantaq.workspace_ids())
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

-- Supabase auto-grants ALL on every new public table to anon, authenticated
-- and service_role (ALTER DEFAULT PRIVILEGES it pre-configures), so the
-- migration's CREATE TABLEs arrive over-granted. Strip that back to the
-- documented ceiling first — anon ends with nothing (not even schema usage),
-- authenticated with exactly the grants below — and trim the default so
-- future migrations don't re-grant anon either. Verified against a live
-- project: without this block, anon reads return 200 [] (RLS-filtered)
-- instead of permission denied.
revoke all on all tables in schema public from anon, authenticated;
revoke usage on schema public from anon;
alter default privileges in schema public revoke all on tables from anon;

alter table workspaces      enable row level security;
alter table projects        enable row level security;
alter table tickets         enable row level security;
alter table comments        enable row level security;
alter table ticket_relationships enable row level security;
alter table members         enable row level security;
alter table tokens          enable row level security;
alter table audit_events    enable row level security;
alter table agent_proposals enable row level security;
alter table memory_entries  enable row level security;
alter table memory_links    enable row level security;
alter table devices         enable row level security;
alter table capability_grants enable row level security;
alter table skill_containers enable row level security;
alter table skill_mappings  enable row level security;
alter table conflict_records enable row level security;
alter table audit_anchors    enable row level security;
alter table milestones       enable row level security;
alter table ticket_milestones enable row level security;

grant select, insert, update         on workspaces      to authenticated;
grant select, insert, update, delete on projects        to authenticated;
grant select, insert, update, delete on tickets         to authenticated;
grant select, insert, update, delete on comments        to authenticated;
-- ticket_relationships are immutable edges (created + deleted, never patched),
-- so no update grant — the missing verb is the rule, like audit_events.
grant select, insert, delete         on ticket_relationships to authenticated;
grant select, insert, update         on members         to authenticated;
grant select, insert, update         on tokens          to authenticated;
grant select, insert                 on audit_events    to authenticated;
grant select, insert, update         on agent_proposals to authenticated;
grant select, insert, update, delete on memory_entries  to authenticated;
grant select, insert, update, delete on memory_links    to authenticated;
-- Trust-root and grant tables are READ-ONLY for clients (E27 adversarial
-- review): a client that could insert/update devices could register itself
-- as a verification root, and one that could update capability_grants could
-- clear revoked_at. Writes arrive only through the verified ingestion path
-- (service_role; signature+grant checks land with E24-T5, Sprint 4).
grant select on devices           to authenticated;
grant select on capability_grants to authenticated;
-- The skill registry (E17 v0.2) is READ-ONLY for clients: v0.2 manages the
-- registry locally (off the sync allowlist) / via the verified path, so the
-- Supabase tables are scaffolding for the future cross-replica sync and carry
-- no insert/update/delete grant — write paths deferred (see the section below).
grant select on skill_containers to authenticated;
grant select on skill_mappings   to authenticated;
-- conflict_records (E05-T2) is READ-ONLY for clients: authoritative_tx, minted
-- and resolved only through the verified RPC path, never a direct client write.
grant select on conflict_records to authenticated;
-- audit_anchors (E07-T5) are append-only AT THE DATABASE, like audit_events:
-- INSERT-as-self + SELECT, and no UPDATE/DELETE grant for any client role.
grant select, insert                 on audit_anchors   to authenticated;
-- milestones (E14 v0.3) are full CRUD like tickets (Member+ writes, enforced by
-- the app role check; RLS scopes to the project's workspace). The junction is
-- create+delete only — a membership is never patched (like ticket_relationships).
grant select, insert, update, delete on milestones      to authenticated;
grant select, insert, delete         on ticket_milestones to authenticated;

grant all on all tables in schema public to service_role;

-- ---------------------------------------------------------------------------
-- workspaces — visible to members; updated by admins; any signed-in user may
-- create one (they become its Owner via the members bootstrap below).
-- ---------------------------------------------------------------------------

drop policy if exists workspaces_select on workspaces;
create policy workspaces_select on workspaces
  for select to authenticated
  using (kantaq.is_member(id));

drop policy if exists workspaces_insert on workspaces;
create policy workspaces_insert on workspaces
  for insert to authenticated
  with check (true);

drop policy if exists workspaces_update on workspaces;
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

drop policy if exists members_select on members;
create policy members_select on members
  for select to authenticated
  using (kantaq.is_member(workspace_id));

drop policy if exists members_insert on members;
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

drop policy if exists members_update on members;
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

drop policy if exists projects_select on projects;
create policy projects_select on projects
  for select to authenticated
  using (kantaq.is_member(workspace_id));

drop policy if exists projects_insert on projects;
create policy projects_insert on projects
  for insert to authenticated
  with check (kantaq.is_member(workspace_id));

drop policy if exists projects_update on projects;
create policy projects_update on projects
  for update to authenticated
  using (kantaq.is_member(workspace_id))
  with check (kantaq.is_member(workspace_id));

drop policy if exists projects_delete on projects;
create policy projects_delete on projects
  for delete to authenticated
  using (kantaq.is_member(workspace_id));

-- ---------------------------------------------------------------------------
-- tickets — scoped through their project's workspace. UPDATE's WITH CHECK
-- re-validates project_id directly (not via the ticket's old project), so a
-- ticket cannot be moved into another workspace's project.
-- ---------------------------------------------------------------------------

drop policy if exists tickets_select on tickets;
create policy tickets_select on tickets
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(id));

drop policy if exists tickets_insert on tickets;
create policy tickets_insert on tickets
  for insert to authenticated
  with check (
    exists (
      select 1 from projects p
      where p.id = project_id and kantaq.is_member(p.workspace_id)
    )
  );

drop policy if exists tickets_update on tickets;
create policy tickets_update on tickets
  for update to authenticated
  using (kantaq.ticket_in_my_workspaces(id))
  with check (
    exists (
      select 1 from projects p
      where p.id = project_id and kantaq.is_member(p.workspace_id)
    )
  );

drop policy if exists tickets_delete on tickets;
create policy tickets_delete on tickets
  for delete to authenticated
  using (kantaq.ticket_in_my_workspaces(id));

-- ---------------------------------------------------------------------------
-- comments — readable across the workspace; written AS yourself (authorship
-- is attribution, like audit), and edited/deleted only by their author.
-- ---------------------------------------------------------------------------

drop policy if exists comments_select on comments;
create policy comments_select on comments
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id));

drop policy if exists comments_insert on comments;
create policy comments_insert on comments
  for insert to authenticated
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and author_actor_id in (select kantaq.member_ids())
  );

drop policy if exists comments_update on comments;
create policy comments_update on comments
  for update to authenticated
  using (author_actor_id in (select kantaq.member_ids()))
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and author_actor_id in (select kantaq.member_ids())
  );

drop policy if exists comments_delete on comments;
create policy comments_delete on comments
  for delete to authenticated
  using (author_actor_id in (select kantaq.member_ids()));

-- ---------------------------------------------------------------------------
-- ticket_relationships (E12-T3) — typed ticket edges, scoped through their
-- endpoints' workspace. SELECT/DELETE gate on the from-ticket; INSERT requires
-- BOTH endpoints to be in my workspaces (so an edge can never reach across the
-- workspace boundary) AND the row written AS yourself (attribution, like
-- comments). No UPDATE policy — an edge is immutable (re-typed by delete +
-- recreate), the database half of the service's create/tombstone-only rule.
-- ---------------------------------------------------------------------------

drop policy if exists ticket_relationships_select on ticket_relationships;
create policy ticket_relationships_select on ticket_relationships
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(from_id));

drop policy if exists ticket_relationships_insert on ticket_relationships;
create policy ticket_relationships_insert on ticket_relationships
  for insert to authenticated
  with check (
    kantaq.ticket_in_my_workspaces(from_id)
    and kantaq.ticket_in_my_workspaces(to_id)
    and created_by in (select kantaq.member_ids())
  );

drop policy if exists ticket_relationships_delete on ticket_relationships;
create policy ticket_relationships_delete on ticket_relationships
  for delete to authenticated
  using (kantaq.ticket_in_my_workspaces(from_id));

-- ---------------------------------------------------------------------------
-- milestones (E14-T2) — scoped through their project's workspace, exactly like
-- tickets. SELECT/DELETE gate on the milestone's project; INSERT/UPDATE's WITH
-- CHECK re-validates project_id directly (so a milestone cannot be moved into
-- another workspace's project). Role (Member+ writes) is enforced by the app.
-- ---------------------------------------------------------------------------

drop policy if exists milestones_select on milestones;
create policy milestones_select on milestones
  for select to authenticated
  using (kantaq.project_in_my_workspaces(project_id));

drop policy if exists milestones_insert on milestones;
create policy milestones_insert on milestones
  for insert to authenticated
  with check (
    exists (
      select 1 from projects p
      where p.id = project_id and kantaq.is_member(p.workspace_id)
    )
  );

drop policy if exists milestones_update on milestones;
create policy milestones_update on milestones
  for update to authenticated
  using (kantaq.project_in_my_workspaces(project_id))
  with check (
    exists (
      select 1 from projects p
      where p.id = project_id and kantaq.is_member(p.workspace_id)
    )
  );

drop policy if exists milestones_delete on milestones;
create policy milestones_delete on milestones
  for delete to authenticated
  using (kantaq.project_in_my_workspaces(project_id));

-- ---------------------------------------------------------------------------
-- ticket_milestones (E14-T2) — the ticket↔milestone junction, scoped through
-- both endpoints' workspace. SELECT/DELETE gate on the ticket; INSERT requires
-- BOTH the ticket AND the milestone to be in my workspaces (so a membership can
-- never reach across the workspace boundary) AND the row written AS yourself.
-- No UPDATE policy — a membership is immutable (re-targeted by delete + insert),
-- the database half of the service's create/tombstone-only rule.
-- ---------------------------------------------------------------------------

drop policy if exists ticket_milestones_select on ticket_milestones;
create policy ticket_milestones_select on ticket_milestones
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id));

drop policy if exists ticket_milestones_insert on ticket_milestones;
create policy ticket_milestones_insert on ticket_milestones
  for insert to authenticated
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and kantaq.milestone_in_my_workspaces(milestone_id)
    and created_by in (select kantaq.member_ids())
  );

drop policy if exists ticket_milestones_delete on ticket_milestones;
create policy ticket_milestones_delete on ticket_milestones
  for delete to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id));

-- ---------------------------------------------------------------------------
-- agent_proposals — readable across the workspace; proposed AS yourself.
-- UPDATE stays workspace-coarse: approve/reject status flips are made by
-- members other than the proposer (the approval flow). proposer_id being
-- mutable post-insert by workspace members is accepted for v0.0.5 and closes
-- with v0.1 event signing (DEBT-01). Never client-deleted.
-- ---------------------------------------------------------------------------

drop policy if exists agent_proposals_select on agent_proposals;
create policy agent_proposals_select on agent_proposals
  for select to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id));

drop policy if exists agent_proposals_insert on agent_proposals;
create policy agent_proposals_insert on agent_proposals
  for insert to authenticated
  with check (
    kantaq.ticket_in_my_workspaces(ticket_id)
    and proposer_id in (select kantaq.member_ids())
  );

drop policy if exists agent_proposals_update on agent_proposals;
create policy agent_proposals_update on agent_proposals
  for update to authenticated
  using (kantaq.ticket_in_my_workspaces(ticket_id))
  with check (kantaq.ticket_in_my_workspaces(ticket_id));

-- ---------------------------------------------------------------------------
-- tokens — by member: a member touches only their own token rows; workspace
-- admins manage everyone's (invite/revoke/rotate, E06). Hashes only ever sit
-- here (argon2id PHC); no delete policy — revoked rows are kept for audit.
-- ---------------------------------------------------------------------------

drop policy if exists tokens_select on tokens;
create policy tokens_select on tokens
  for select to authenticated
  using (
    member_id in (select kantaq.member_ids())
    or kantaq.is_admin_of_member(member_id)
  );

drop policy if exists tokens_insert on tokens;
create policy tokens_insert on tokens
  for insert to authenticated
  with check (
    member_id in (select kantaq.member_ids())
    or kantaq.is_admin_of_member(member_id)
  );

drop policy if exists tokens_update on tokens;
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

drop policy if exists audit_events_select on audit_events;
create policy audit_events_select on audit_events
  for select to authenticated
  using (kantaq.actor_in_my_workspaces(actor_id));

drop policy if exists audit_events_insert on audit_events;
create policy audit_events_insert on audit_events
  for insert to authenticated
  with check (actor_id in (select kantaq.member_ids()));

-- ---------------------------------------------------------------------------
-- memory_entries (E13, MOD-19) — only team-visibility rows may ever exist at
-- the backend: visibility = 'team' rides every policy as the database layer
-- of NFR-E13-1 (local entries never sync; the emit seam in kantaq_core.memory
-- is the first wall, this is the last). SELECT is workspace-coarse via the
-- creator's membership; INSERT as yourself (attribution, like comments);
-- UPDATE/DELETE by the author only until Sprint 4 wires the memory sync flow.
-- ---------------------------------------------------------------------------

drop policy if exists memory_entries_select on memory_entries;
create policy memory_entries_select on memory_entries
  for select to authenticated
  using (visibility = 'team' and kantaq.actor_in_my_workspaces(created_by));

drop policy if exists memory_entries_insert on memory_entries;
create policy memory_entries_insert on memory_entries
  for insert to authenticated
  with check (visibility = 'team' and created_by in (select kantaq.member_ids()));

drop policy if exists memory_entries_update on memory_entries;
create policy memory_entries_update on memory_entries
  for update to authenticated
  using (created_by in (select kantaq.member_ids()))
  with check (visibility = 'team' and created_by in (select kantaq.member_ids()));

drop policy if exists memory_entries_delete on memory_entries;
create policy memory_entries_delete on memory_entries
  for delete to authenticated
  using (created_by in (select kantaq.member_ids()));

-- ---------------------------------------------------------------------------
-- memory_links (E13) — scoped through their ticket like comments; created AS
-- yourself; the same visibility = 'team' wall (a link to a local entry is
-- itself local and must never exist here). Author-only delete.
-- ---------------------------------------------------------------------------

drop policy if exists memory_links_select on memory_links;
create policy memory_links_select on memory_links
  for select to authenticated
  using (visibility = 'team' and kantaq.ticket_in_my_workspaces(ticket_id));

drop policy if exists memory_links_insert on memory_links;
create policy memory_links_insert on memory_links
  for insert to authenticated
  with check (
    visibility = 'team'
    and kantaq.ticket_in_my_workspaces(ticket_id)
    and created_by in (select kantaq.member_ids())
  );

drop policy if exists memory_links_update on memory_links;
create policy memory_links_update on memory_links
  for update to authenticated
  using (created_by in (select kantaq.member_ids()))
  with check (visibility = 'team' and created_by in (select kantaq.member_ids()));

drop policy if exists memory_links_delete on memory_links;
create policy memory_links_delete on memory_links
  for delete to authenticated
  using (created_by in (select kantaq.member_ids()));

-- ---------------------------------------------------------------------------
-- devices (E06 v0.1) — a runtime's Ed25519 verify key. Workspace members may
-- read every device (the roots grant verification resolves against); clients
-- can never write a trust root directly. Never deleted (revoked_at flips
-- instead): a vanished root would orphan signatures.
-- ---------------------------------------------------------------------------

drop policy if exists devices_select on devices;
create policy devices_select on devices
  for select to authenticated
  using (member_id is null or kantaq.actor_in_my_workspaces(member_id));

-- No insert/update policies for authenticated: device registration goes
-- through the verified service-role path only (see the grant block above).

-- ---------------------------------------------------------------------------
-- capability_grants (E06 v0.1) — signed permission slips (authoritative_tx;
-- the backend verifies signature + grant before accepting events in Sprint 4,
-- E24-T5). Members read grants in their workspaces; clients can never write
-- one directly. Never deleted: revoked rows stay for audit, like tokens.
-- ---------------------------------------------------------------------------

drop policy if exists capability_grants_select on capability_grants;
create policy capability_grants_select on capability_grants
  for select to authenticated
  using (kantaq.actor_in_my_workspaces(subject));

-- No insert/update policies for authenticated: grants are authoritative_tx
-- and reach the backend only through the verified path (E24-T5, Sprint 4).

-- ---------------------------------------------------------------------------
-- skill_containers / skill_mappings (E17 v0.2, MOD-22) — the db-backed skill
-- registry. v0.2 manages the registry LOCALLY: both collections are OFF the
-- sync allowlist (architecture §6.1 "backend registry"; see
-- tests/test_sync_allowlists.py NEVER_SYNC), so the registry CRUD service
-- (kantaq_core.skills) writes locally + audited and never emits a sync event.
-- The Supabase tables and the RLS below are SCAFFOLDING for the future
-- cross-replica registry sync; write paths are deferred (no insert/update/
-- delete grant above), so only SELECT policies exist here.
--
-- skill_containers — the lifecycle taxonomy is global, non-sensitive reference
-- data: readable by every signed-in member. skill_mappings — workspace
-- mappings are readable by all members; personal mappings only by their owner.
-- ---------------------------------------------------------------------------

drop policy if exists skill_containers_select on skill_containers;
create policy skill_containers_select on skill_containers
  for select to authenticated
  using (true);

drop policy if exists skill_mappings_select on skill_mappings;
create policy skill_mappings_select on skill_mappings
  for select to authenticated
  using (scope = 'workspace' or created_by in (select kantaq.member_ids()));

-- ---------------------------------------------------------------------------
-- conflict_records (E05-T2, MOD-26 §B4) — resolvable same-scalar conflicts,
-- minted at the authoritative merge (authoritative_tx, backend authority).
-- Members read the conflicts in their workspaces; clients never write one
-- directly — both the mint and the resolution reach the backend only through
-- the verified RPC path, like capability_grants. No insert/update/delete grant
-- above, so only a SELECT policy exists here.
-- ---------------------------------------------------------------------------

drop policy if exists conflict_records_select on conflict_records;
create policy conflict_records_select on conflict_records
  for select to authenticated
  using (kantaq.is_member(workspace_id));

-- ---------------------------------------------------------------------------
-- audit_anchors (E07-T5, MOD-07 / FR-E07-5) — append-only AT THE DATABASE,
-- exactly like audit_events: INSERT only as yourself, SELECT inside the actor's
-- workspace, and no UPDATE/DELETE policy for any client role (not even Owners).
-- An anchor is a content-free integrity commitment (range ids + a Merkle root)
-- over a range of the actor's own audit trail; the runtime writes it before
-- retention prunes, and the immutability here is the server half of "the anchor
-- still proves the pre-retention range".
-- ---------------------------------------------------------------------------

drop policy if exists audit_anchors_select on audit_anchors;
create policy audit_anchors_select on audit_anchors
  for select to authenticated
  using (kantaq.actor_in_my_workspaces(actor_id));

drop policy if exists audit_anchors_insert on audit_anchors;
create policy audit_anchors_insert on audit_anchors
  for insert to authenticated
  with check (actor_id in (select kantaq.member_ids()));
