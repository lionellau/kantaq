-- supabase/migrations/0001_collections.sql
-- GENERATED from SQLModel.metadata (kantaq_db.models) — do not edit by hand.
-- Regenerate: uv run python -m kantaq_backend_supabase.schema
-- The 8 v0.0.5 collections, 1:1 with the local replica (D-07 parity, schema v2).
-- Apply RLS afterwards: supabase/policies/0001_rls.sql (RLS is not optional).


CREATE TABLE audit_anchors (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	actor_id VARCHAR NOT NULL,
	range_start VARCHAR(26) NOT NULL,
	range_end VARCHAR(26) NOT NULL,
	merkle_root VARCHAR(64) NOT NULL,
	tree_size INTEGER NOT NULL,
	chain_tip VARCHAR(64) NOT NULL,
	external_pin VARCHAR,
	PRIMARY KEY (id)
);

CREATE INDEX ix_audit_anchors_actor_id ON audit_anchors (actor_id);

CREATE INDEX ix_audit_anchors_range_end ON audit_anchors (range_end);

CREATE INDEX ix_audit_anchors_range_start ON audit_anchors (range_start);

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

CREATE TABLE memory_entries (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	title VARCHAR NOT NULL,
	body VARCHAR NOT NULL,
	type VARCHAR(16) NOT NULL,
	source VARCHAR(16) NOT NULL,
	space VARCHAR(16) NOT NULL,
	linked_entities JSON NOT NULL,
	provenance JSON NOT NULL,
	confidence VARCHAR(8) NOT NULL,
	review_status VARCHAR(16) NOT NULL,
	expires_at TIMESTAMP WITHOUT TIME ZONE,
	created_by VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE skill_containers (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	slug VARCHAR(32) NOT NULL,
	name VARCHAR(64) NOT NULL,
	recommended_roles JSON NOT NULL,
	supported_stages JSON NOT NULL,
	required_input VARCHAR NOT NULL,
	expected_output VARCHAR NOT NULL,
	allowed_tools JSON NOT NULL,
	default_write_mode VARCHAR(16) NOT NULL,
	risk_level VARCHAR(16) NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_skill_containers_slug ON skill_containers (slug);

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

CREATE TABLE conflict_records (
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

CREATE INDEX ix_conflict_records_entity_id ON conflict_records (entity_id);

CREATE INDEX ix_conflict_records_workspace_id ON conflict_records (workspace_id);

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

CREATE TABLE skill_mappings (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	container_id VARCHAR NOT NULL,
	scope VARCHAR(16) NOT NULL,
	provider VARCHAR NOT NULL,
	connection VARCHAR NOT NULL,
	status VARCHAR(16) NOT NULL,
	created_by VARCHAR,
	PRIMARY KEY (id),
	FOREIGN KEY(container_id) REFERENCES skill_containers (id)
);

CREATE INDEX ix_skill_mappings_container_id ON skill_mappings (container_id);

CREATE TABLE devices (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	public_key VARCHAR(64) NOT NULL,
	member_id VARCHAR,
	label VARCHAR NOT NULL,
	revoked_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (public_key),
	FOREIGN KEY(member_id) REFERENCES members (id)
);

CREATE INDEX ix_devices_member_id ON devices (member_id);

CREATE TABLE milestones (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	project_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	description VARCHAR NOT NULL,
	target_date TIMESTAMP WITHOUT TIME ZONE,
	status VARCHAR(16) NOT NULL,
	created_by VARCHAR,
	PRIMARY KEY (id),
	FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_milestones_project_id ON milestones (project_id);

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
	attachments JSON NOT NULL,
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

CREATE TABLE capability_grants (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	subject VARCHAR NOT NULL,
	issuer VARCHAR NOT NULL,
	resource VARCHAR NOT NULL,
	verbs JSON NOT NULL,
	issued_at BIGINT NOT NULL,
	expires_at BIGINT NOT NULL,
	revokes VARCHAR,
	sig VARCHAR(128),
	token_id VARCHAR,
	revoked_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(subject) REFERENCES members (id),
	FOREIGN KEY(issuer) REFERENCES devices (id),
	FOREIGN KEY(token_id) REFERENCES tokens (id)
);

CREATE INDEX ix_capability_grants_issuer ON capability_grants (issuer);

CREATE INDEX ix_capability_grants_subject ON capability_grants (subject);

CREATE INDEX ix_capability_grants_token_id ON capability_grants (token_id);

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

CREATE TABLE memory_links (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	ticket_id VARCHAR NOT NULL,
	memory_id VARCHAR NOT NULL,
	reason VARCHAR NOT NULL,
	created_by VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT uq_memory_link_pair UNIQUE (ticket_id, memory_id),
	FOREIGN KEY(ticket_id) REFERENCES tickets (id),
	FOREIGN KEY(memory_id) REFERENCES memory_entries (id)
);

CREATE INDEX ix_memory_links_memory_id ON memory_links (memory_id);

CREATE INDEX ix_memory_links_ticket_id ON memory_links (ticket_id);

CREATE TABLE ticket_milestones (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	ticket_id VARCHAR NOT NULL,
	milestone_id VARCHAR NOT NULL,
	created_by VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT uq_ticket_milestone UNIQUE (ticket_id, milestone_id),
	FOREIGN KEY(ticket_id) REFERENCES tickets (id),
	FOREIGN KEY(milestone_id) REFERENCES milestones (id)
);

CREATE INDEX ix_ticket_milestones_milestone_id ON ticket_milestones (milestone_id);

CREATE INDEX ix_ticket_milestones_ticket_id ON ticket_milestones (ticket_id);

CREATE TABLE ticket_relationships (
	id VARCHAR(26) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	actor_seq INTEGER NOT NULL,
	visibility VARCHAR(16) NOT NULL,
	hosting_mode VARCHAR(16) NOT NULL,
	retention_policy VARCHAR(16) NOT NULL,
	from_id VARCHAR NOT NULL,
	to_id VARCHAR NOT NULL,
	type VARCHAR(16) NOT NULL,
	created_by VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT uq_ticket_relationship UNIQUE (from_id, to_id, type),
	FOREIGN KEY(from_id) REFERENCES tickets (id),
	FOREIGN KEY(to_id) REFERENCES tickets (id)
);

CREATE INDEX ix_ticket_relationships_from_id ON ticket_relationships (from_id);

CREATE INDEX ix_ticket_relationships_to_id ON ticket_relationships (to_id);
