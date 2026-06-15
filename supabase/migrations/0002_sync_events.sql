-- supabase/migrations/0002_sync_events.sql
-- HAND-WRITTEN (E24-T4, FR-E24-1): the shared sync event log.
-- Apply AFTER 0001_collections.sql; apply RLS afterwards:
-- supabase/policies/0002_sync_rls.sql (RLS is not optional).
--
-- This table is the backend half of the MOD-04 backend port: push INSERTs
-- protocol events here, pull SELECTs them back in commit order. It is
-- deliberately NOT generated from SQLModel.metadata (D-07 covers the 8
-- collection mirrors; the local event_log/sync_cursors tables are replica
-- infrastructure and never mirror to the backend, MOD-04 "Data"). The shapes
-- still match the wire object field-for-field (kantaq_sync_engine.events.Event)
-- so nothing reshapes between the local log and this one.
--
-- Commit order (D-05): `revision` is a Postgres identity column — strictly
-- monotonic, assigned by the backend at INSERT, never by client clocks.
-- Last-writer-wins folds by this order. Known v0.0.5 limit (accepted, closes
-- with the v0.2 atomic plpgsql RPC, D-09): under concurrent pushes a reader
-- can observe revision N+1 before an in-flight N commits; a 2 s polling
-- cadence and human-scale write rates make the window negligible for dogfood.
--
-- Dedup (NFR-E04-2): UNIQUE (actor_id, actor_seq) is the hard floor. Push
-- targets it with ON CONFLICT DO NOTHING (PostgREST `resolution=
-- ignore-duplicates`), so a retry can never double-commit.

CREATE TABLE sync_events (
	revision BIGINT GENERATED ALWAYS AS IDENTITY,
	event_id VARCHAR(26) NOT NULL,
	collection VARCHAR(32) NOT NULL,
	entity_id VARCHAR(26) NOT NULL,
	actor_id VARCHAR(26) NOT NULL,
	actor_seq INTEGER NOT NULL,
	op VARCHAR(16) NOT NULL,
	base_rev BIGINT,
	policy_ref VARCHAR,
	payload JSON NOT NULL,
	sig VARCHAR,
	workspace_id VARCHAR(26) NOT NULL,
	committed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
	PRIMARY KEY (revision),
	UNIQUE (event_id),
	UNIQUE (actor_id, actor_seq),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
	-- The protocol op vocabulary (architecture §6).
	CONSTRAINT ck_sync_events_op CHECK (op IN ('patch', 'append', 'tombstone')),
	-- The syncable-collection allowlist, enforced at the database (MOD-04
	-- "Syncable collections"): tokens never sync (authority local, secret
	-- material) and audit_events are each replica's own local trail — even a
	-- tampered client cannot push either into the shared log.
	-- memory_entries/memory_links joined with E13 (team-visibility rows only;
	-- local rows never produce events at all — the MOD-19 emit seam).
	-- ticket_relationships joined with E12-T3 (typed ticket edges, MOD-03 v0.1).
	-- devices and capability_grants joined with E24-T7 (v0.2): now that the
	-- backend verifies signatures + grants before accepting events (E24-T5
	-- client-side + the E24-T6 atomic RPC server-side), the trust roots ingest
	-- so teammates receive each other's device keys and grants. On PULL these
	-- route to the identity store, not the domain fold (DEBT-21). Keep this
	-- list, the local applier's SYNCABLE_MODELS, and COLLECTION_META in
	-- lock-step — tests/test_sync_allowlists.py pins all three against each
	-- other (and the README ALTER note against this CHECK).
	-- conflict_records joined with E05-T2 (MOD-26 §B4): authoritative_tx, minted
	-- at the merge; on PULL it routes to its own ingest, not the domain fold.
	CONSTRAINT ck_sync_events_collection CHECK (collection IN
		('workspaces', 'projects', 'tickets', 'comments', 'ticket_relationships',
		 'members', 'agent_proposals', 'memory_entries', 'memory_links',
		 'devices', 'capability_grants', 'conflict_records'))
);

CREATE INDEX ix_sync_events_collection ON sync_events (collection, revision);

CREATE INDEX ix_sync_events_workspace_id ON sync_events (workspace_id, revision);
