-- supabase/migrations/0003_sync_acks.sql
-- HAND-WRITTEN (E07-T4 / FR-E26-2, MOD-05 + MOD-27): the server-side ack
-- watermark that makes sync_events retention cursor-safe. Apply AFTER
-- 0002_sync_events.sql; apply RLS afterwards (policies/0005_sync_acks_rls.sql).
--
-- WHY THIS EXISTS. sync_events compaction cannot be wall-clock-only. Replica
-- pull cursors track by `revision`, not `committed_at`, and sync_cursors are
-- LOCAL per replica (never synced), so a backend prune cannot see them. A naive
-- DELETE .. WHERE committed_at < now()-30d would strand any replica whose acked
-- revision still lags the 30-day-old tail — silent data loss or a permanently
-- stuck pull. This table is the backend's view of each replica's acked revision,
-- so kantaq.compact_sync_events (rpc/retention.sql) can compute
-- safe_watermark_rev = MIN(acked_rev) across LIVE replicas and never delete a
-- row a live replica still needs.
--
-- Not a D-07 collection mirror (like sync_events, this is backend
-- infrastructure, deliberately hand-written and OFF the sync allowlist). One row
-- per (workspace_id, replica_id); the runtime upserts its own row on each ack,
-- keyed by its local actor/device id. A replica silent past the retention TTL is
-- excluded from the watermark by the compaction and re-snapshots (MOD-26
-- snapshot-then-stream) rather than holding the prune back forever.

CREATE TABLE sync_acks (
	workspace_id VARCHAR(26) NOT NULL,
	member_id VARCHAR(26) NOT NULL,
	replica_id VARCHAR(26) NOT NULL,
	acked_rev BIGINT NOT NULL DEFAULT 0,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
	-- member_id is in the key (not just replica_id) so RLS can bind every ack row
	-- to the member that wrote it: a member can only create/move rows attributed
	-- to themselves, so it can never occupy or over-report a peer's slot (the
	-- E07-T4 cross-member stranding hole). A peer's real ack always records under
	-- its own (member_id, replica_id) key regardless of any look-alike row.
	PRIMARY KEY (workspace_id, member_id, replica_id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX ix_sync_acks_workspace ON sync_acks (workspace_id, acked_rev);
