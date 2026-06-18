-- supabase/deploy/conflict_records_catchup.sql
-- v0.2 live catch-up: add the conflict_records collection to an existing
-- project (E05-T2, MOD-26 §B4). Brings a project that predates conflict_records
-- (15 collections, sync allowlist = 11) up to repo parity (16 collections,
-- allowlist = 12). The other v0.2 delta — the events.sql p_cas re-apply — is a
-- SEPARATE step and is already live as of the DEBT-25 deploy.
--
-- SOURCE: every statement here is copied verbatim from the checked-in artifacts
-- (schema-SOP — the agent authors, a human applies):
--   * table + indexes  -> migrations/0001_collections.sql
--   * RLS enable/grant/policy -> policies/0001_rls.sql
--   * sync allowlist 11->12   -> migrations/0002_sync_events.sql (ck constraint)
-- The kantaq.* helpers (is_member) the policy uses already exist live (0001_rls).
--
-- SAFE + ADDITIVE: conflict_records is authoritative_tx — backend-write-only,
-- read-only for clients (mirrors capability_grants), so adding it cannot break
-- any running client; widening the CHECK only *permits* a new collection.
-- Idempotent: re-runnable as-is (IF NOT EXISTS / DROP ... IF EXISTS).
--
-- APPLY: paste into the Supabase SQL Editor and Run (a human applies this to the
-- live shared project — no agent writes to the live backend). Then run the
-- verification block at the bottom.

-- 1. The collection table + its indexes (migrations/0001_collections.sql).
CREATE TABLE IF NOT EXISTS conflict_records (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	workspace_id VARCHAR NOT NULL,
	collection VARCHAR(32) NOT NULL,
	entity_id VARCHAR(26) NOT NULL,
	field VARCHAR(64) NOT NULL,
	contending_revisions JSON NOT NULL,
	candidate_values JSON NOT NULL,
	base_rev INTEGER NOT NULL,
	head_rev INTEGER NOT NULL,
	actor VARCHAR(26) NOT NULL,
	status VARCHAR(16) NOT NULL,
	resolved_by VARCHAR(26),
	resolved_choice VARCHAR(16),
	resolved_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_conflict_records_entity_id ON conflict_records (entity_id);

CREATE INDEX IF NOT EXISTS ix_conflict_records_workspace_id ON conflict_records (workspace_id);

-- 2. RLS: read-only for clients — members read their workspaces' conflicts; the
--    mint + resolution arrive only through the verified RPC path, like
--    capability_grants (policies/0001_rls.sql). No insert/update/delete grant.
alter table conflict_records enable row level security;

grant select on conflict_records to authenticated;

drop policy if exists conflict_records_select on conflict_records;
create policy conflict_records_select on conflict_records
  for select to authenticated
  using (kantaq.is_member(workspace_id));

-- 3. Widen the sync allowlist 11 -> 12 (migrations/0002_sync_events.sql). Must
--    byte-match the checked-in ck_sync_events_collection (pinned by
--    tests/test_sync_allowlists.py).
ALTER TABLE sync_events DROP CONSTRAINT IF EXISTS ck_sync_events_collection;
ALTER TABLE sync_events ADD CONSTRAINT ck_sync_events_collection CHECK (collection IN
	('workspaces', 'projects', 'tickets', 'comments', 'ticket_relationships',
	 'members', 'agent_proposals', 'memory_entries', 'memory_links',
	 'devices', 'capability_grants', 'conflict_records'));

-- 4. VERIFY (read-only) — run after applying. Expect, in order:
--    a) one row: conflict_records table exists
--    b) one row: rls_enabled = true
--    c) one row: conflict_records_select policy present
--    d) one row: allowlist_has_conflict_records = true
select 'table' as item, count(*)::text as result
  from information_schema.tables
  where table_schema = 'public' and table_name = 'conflict_records'
union all
select 'rls_enabled', (relrowsecurity)::text
  from pg_class where oid = 'public.conflict_records'::regclass
union all
select 'select_policy', count(*)::text
  from pg_policies
  where schemaname = 'public' and tablename = 'conflict_records'
    and policyname = 'conflict_records_select'
union all
select 'allowlist_has_conflict_records',
       (pg_get_constraintdef(oid) like '%conflict_records%')::text
  from pg_constraint where conname = 'ck_sync_events_collection';
