"""skill registry for E17-T4 (MOD-22 v0.2 / MOD-02)

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-15

Adds the db-backed skill registry (FR-E17-2): ``skill_containers`` (the unit of
agent work) and ``skill_mappings`` (a personal/workspace skill→tool binding).
The migration seeds the 29 hardcoded containers from ``kantaq_core.reco`` into
the registry, moving them "behind the same contract" while the engine stays
pure. The seed values are STATIC LITERALS below — a migration must never import
``kantaq_core`` (its meaning cannot change when code does), so the rows were
dumped once and pasted here with fixed ULIDs and a deterministic timestamp.

Both collections are db-backed but OFF the sync allowlist in v0.2 (architecture
§6.1 "backend registry"); the table treatment is full (model/parity/Supabase
DDL/RLS) so the future cross-replica sync is a one-line allowlist change.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _envelope() -> list[sa.Column[object]]:
    """The CollectionBase envelope, identical to every collection table."""
    return [
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("actor_seq", sa.Integer(), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("hosting_mode", sa.String(length=16), nullable=False),
        sa.Column("retention_policy", sa.String(length=16), nullable=False),
    ]


def _write_version(version: int, rev: str) -> None:
    op.execute(sa.text("DELETE FROM schema_version"))
    op.bulk_insert(
        sa.table(
            "schema_version",
            sa.column("version", sa.Integer),
            sa.column("revision", sa.String),
            sa.column("applied_at", sa.DateTime),
        ),
        [{"version": version, "revision": rev, "applied_at": datetime.now(UTC)}],
    )


# A deterministic seed timestamp so the migration is reproducible across runs.
_SEED_TS = datetime(2026, 1, 1, tzinfo=UTC)

# The 29 v0.1 skill containers, dumped once from kantaq_core.reco.CONTAINERS and
# pasted as static literals (ids are fixed ULIDs). ``recommended_roles`` is the
# plural JSON list of the single v0.1 ``recommended_role``. The migration never
# imports kantaq_core: a migration's meaning is frozen at write time.
_CONTAINER_SEED: list[dict[str, object]] = [
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PQ",
        "slug": "triage",
        "name": "Triage",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["intake"],
        "expected_output": "a triaged ticket: type, priority, and any duplicates flagged",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PR",
        "slug": "issue-shaping",
        "name": "Issue shaping",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["intake"],
        "expected_output": "a shaped ticket: a clear problem statement and acceptance criteria",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PS",
        "slug": "duplicate-detection",
        "name": "Duplicate detection",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["intake"],
        "expected_output": "a duplicate scan: links to similar existing tickets",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PT",
        "slug": "product-framing",
        "name": "Product framing",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["discovery"],
        "expected_output": "a problem framing: the user need, scope, and success signals",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PV",
        "slug": "user-research",
        "name": "User research",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["discovery"],
        "expected_output": "a research summary: who is affected and what they need",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PW",
        "slug": "requirement-synthesis",
        "name": "Requirement synthesis",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["discovery"],
        "expected_output": "synthesised requirements and acceptance criteria",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PX",
        "slug": "project-planning",
        "name": "Project planning",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["planning"],
        "expected_output": "a plan: milestones, risks, and sequencing",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PY",
        "slug": "technical-decomposition",
        "name": "Technical decomposition",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["planning"],
        "expected_output": "a technical breakdown into sub-tickets with dependencies",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0PZ",
        "slug": "dependency-mapping",
        "name": "Dependency mapping",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["planning"],
        "expected_output": "a dependency map across the affected components",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q0",
        "slug": "ux-design",
        "name": "UX design",
        "recommended_roles": ["design_agent"],
        "supported_stages": ["design"],
        "expected_output": "UX flows and states for the ticket",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q1",
        "slug": "design-system",
        "name": "Design system",
        "recommended_roles": ["design_agent"],
        "supported_stages": ["design"],
        "expected_output": "the design-system tokens and components that apply",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q2",
        "slug": "accessibility-review",
        "name": "Accessibility review",
        "recommended_roles": ["design_agent"],
        "supported_stages": ["design"],
        "expected_output": "an accessibility audit with WCAG findings",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q3",
        "slug": "design-review",
        "name": "Design review",
        "recommended_roles": ["design_agent"],
        "supported_stages": ["design"],
        "expected_output": "a design critique against the system and accessibility",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q4",
        "slug": "repo-investigation",
        "name": "Repo investigation",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["implementation"],
        "expected_output": "a map of the code paths this change touches",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q5",
        "slug": "code-agent",
        "name": "Code agent",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["implementation"],
        "expected_output": "a proposed code change, queued behind the approval gate",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "high",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q6",
        "slug": "test-generation",
        "name": "Test generation",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["implementation"],
        "expected_output": "proposed tests covering the acceptance criteria",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q7",
        "slug": "code-review",
        "name": "Code review",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["review"],
        "expected_output": "a code review: correctness, security, and maintainability findings",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q8",
        "slug": "security-review",
        "name": "Security review",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["review"],
        "expected_output": "a security review: injection, authz, and data-exposure findings",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "high",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0Q9",
        "slug": "architecture-review",
        "name": "Architecture review",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["review"],
        "expected_output": "an architecture review against the recorded decisions",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QA",
        "slug": "browser-qa",
        "name": "Browser QA",
        "recommended_roles": ["qa_agent"],
        "supported_stages": ["qa"],
        "expected_output": "a QA run: behaviours verified and regressions found",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QB",
        "slug": "regression-testing",
        "name": "Regression testing",
        "recommended_roles": ["qa_agent"],
        "supported_stages": ["qa"],
        "expected_output": "a regression pass result against prior behaviour",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QC",
        "slug": "bug-triage",
        "name": "Bug triage",
        "recommended_roles": ["qa_agent"],
        "supported_stages": ["qa"],
        "expected_output": "a triaged bug: severity, reproduction, and owner",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QD",
        "slug": "release-check",
        "name": "Release check",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["release"],
        "expected_output": "a release-readiness checklist result",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QE",
        "slug": "changelog",
        "name": "Changelog",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["release"],
        "expected_output": "a proposed changelog entry",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QF",
        "slug": "docs-update",
        "name": "Docs update",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["release"],
        "expected_output": "proposed documentation updates for the change",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QG",
        "slug": "deployment-check",
        "name": "Deployment check",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["release"],
        "expected_output": "a deployment-readiness check (config, migrations, rollbacks)",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "medium",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QH",
        "slug": "retrospective",
        "name": "Retrospective",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["learn"],
        "expected_output": "a retrospective: what worked and what to change",
        "allowed_tools": ["role_context_get", "ticket_get", "ticket_search", "memory_search"],
        "default_write_mode": "read",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QJ",
        "slug": "decision-log",
        "name": "Decision log",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["learn"],
        "expected_output": "a captured decision with its rationale",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
    {
        "id": "01KV4XA5B4Q6D9RFHNCE6MR0QK",
        "slug": "memory-curation",
        "name": "Memory curation",
        "recommended_roles": ["product_agent"],
        "supported_stages": ["learn"],
        "expected_output": "curated memory: entries to promote or retire",
        "allowed_tools": [
            "role_context_get",
            "ticket_get",
            "ticket_search",
            "memory_search",
            "ticket_comment_create",
            "agent_action_propose",
        ],
        "default_write_mode": "propose",
        "risk_level": "low",
    },
]


def _seed_rows() -> list[dict[str, object]]:
    """The seed dicts with the full collection envelope filled in (deterministic)."""
    rows: list[dict[str, object]] = []
    for spec in _CONTAINER_SEED:
        rows.append(
            {
                **spec,
                "required_input": "",
                "created_at": _SEED_TS,
                "updated_at": _SEED_TS,
                "actor_seq": 0,
                "visibility": "team",
                "hosting_mode": "plain",
                "retention_policy": "standard",
            }
        )
    return rows


def upgrade() -> None:
    op.create_table(
        "skill_containers",
        *_envelope(),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("recommended_roles", sa.JSON(), nullable=False),
        sa.Column("supported_stages", sa.JSON(), nullable=False),
        sa.Column("required_input", sa.String(), nullable=False),
        sa.Column("expected_output", sa.String(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("default_write_mode", sa.String(length=16), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # The model declares ``slug`` as ``Field(unique=True, index=True)``, which
    # SQLModel renders as a single UNIQUE index — not a separate UNIQUE
    # constraint. The migration must match the model 1:1 (the
    # test_migration_matches_models parity gate), so uniqueness is enforced by
    # the unique index alone (no redundant uq_skill_container_slug constraint).
    op.create_index(op.f("ix_skill_containers_slug"), "skill_containers", ["slug"], unique=True)

    op.create_table(
        "skill_mappings",
        *_envelope(),
        sa.Column("container_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("connection", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["container_id"], ["skill_containers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_skill_mappings_container_id"), "skill_mappings", ["container_id"])

    # Seed the 29 hardcoded containers into the registry (static literals above).
    op.bulk_insert(
        sa.table(
            "skill_containers",
            sa.column("id", sa.String),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
            sa.column("actor_seq", sa.Integer),
            sa.column("visibility", sa.String),
            sa.column("hosting_mode", sa.String),
            sa.column("retention_policy", sa.String),
            sa.column("slug", sa.String),
            sa.column("name", sa.String),
            sa.column("recommended_roles", sa.JSON),
            sa.column("supported_stages", sa.JSON),
            sa.column("required_input", sa.String),
            sa.column("expected_output", sa.String),
            sa.column("allowed_tools", sa.JSON),
            sa.column("default_write_mode", sa.String),
            sa.column("risk_level", sa.String),
        ),
        _seed_rows(),
    )

    _write_version(10, "0010")


def downgrade() -> None:
    op.drop_index(op.f("ix_skill_mappings_container_id"), table_name="skill_mappings")
    op.drop_index(op.f("ix_skill_containers_slug"), table_name="skill_containers")
    op.drop_table("skill_mappings")
    op.drop_table("skill_containers")

    _write_version(9, "0009")
