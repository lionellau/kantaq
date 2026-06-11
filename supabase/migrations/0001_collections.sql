-- supabase/migrations/0001_collections.sql
-- GENERATED from SQLModel.metadata (kantaq_db.models) — do not edit by hand.
-- Regenerate: uv run python -m kantaq_backend_supabase.schema
-- The 8 v0.0.5 collections, 1:1 with the local replica (D-07 parity, schema v2).
-- Apply RLS afterwards: supabase/policies/0001_rls.sql (RLS is not optional).


CREATE TABLE audit_events (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	actor_id VARCHAR NOT NULL,
	action VARCHAR(64) NOT NULL,
	object_ref VARCHAR,
	before JSON,
	after JSON,
	source VARCHAR(16) NOT NULL,
	chain_hash VARCHAR,
	PRIMARY KEY (id)
);

CREATE INDEX ix_audit_events_actor_id ON audit_events (actor_id);

CREATE INDEX ix_audit_events_object_ref ON audit_events (object_ref);

CREATE TABLE workspaces (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE members (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	workspace_id VARCHAR NOT NULL,
	email VARCHAR NOT NULL,
	role VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX ix_members_email ON members (email);

CREATE INDEX ix_members_workspace_id ON members (workspace_id);

CREATE TABLE projects (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	workspace_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	goal VARCHAR NOT NULL,
	scope VARCHAR NOT NULL,
	owner VARCHAR,
	target_date TIMESTAMP WITHOUT TIME ZONE,
	status VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX ix_projects_workspace_id ON projects (workspace_id);

CREATE TABLE tickets (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	project_id VARCHAR NOT NULL,
	title VARCHAR NOT NULL,
	description VARCHAR NOT NULL,
	status VARCHAR(32) NOT NULL,
	priority VARCHAR(16) NOT NULL,
	labels JSON NOT NULL,
	assignee VARCHAR,
	due_date TIMESTAMP WITHOUT TIME ZONE,
	acceptance_criteria VARCHAR NOT NULL,
	lifecycle_stage VARCHAR(32) NOT NULL,
	parent_id VARCHAR,
	created_by VARCHAR,
	PRIMARY KEY (id),
	FOREIGN KEY(project_id) REFERENCES projects (id),
	FOREIGN KEY(parent_id) REFERENCES tickets (id)
);

CREATE INDEX ix_tickets_parent_id ON tickets (parent_id);

CREATE INDEX ix_tickets_project_id ON tickets (project_id);

CREATE TABLE tokens (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	member_id VARCHAR NOT NULL,
	hashed VARCHAR NOT NULL,
	scopes JSON NOT NULL,
	revoked_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(member_id) REFERENCES members (id)
);

CREATE INDEX ix_tokens_member_id ON tokens (member_id);

CREATE TABLE agent_proposals (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	ticket_id VARCHAR NOT NULL,
	proposer_id VARCHAR NOT NULL,
	diff JSON NOT NULL,
	status VARCHAR(16) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(ticket_id) REFERENCES tickets (id)
);

CREATE INDEX ix_agent_proposals_ticket_id ON agent_proposals (ticket_id);

CREATE TABLE comments (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	ticket_id VARCHAR NOT NULL,
	author_actor_id VARCHAR NOT NULL,
	body VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(ticket_id) REFERENCES tickets (id)
);

CREATE INDEX ix_comments_ticket_id ON comments (ticket_id);
