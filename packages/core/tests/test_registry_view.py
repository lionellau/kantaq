"""E17-T5: the recommendation engine reads the db-backed registry (FR-E17-2).

The pure engine (test_reco.py) reads the hardcoded tuple; here we inject a
``DbRegistry`` built from db rows and pin two things: (1) a registry that mirrors
the v0.1 containers reproduces the exact same recommendations (same slugs, same
categorical confidence, same roles) — reading the registry changes the *source*,
not the rule; (2) a user's active skill→tool mapping reflects in the output's
``mapped_tool`` (the whole point of v0.2), while a disabled mapping leaves the
role's default hint standing.
"""

from __future__ import annotations

from types import SimpleNamespace

from kantaq_core import reco
from kantaq_core.skills import DbRegistry
from kantaq_db.models import SkillContainerRow, SkillMappingRow


def _ticket(stage: str = "implementation", labels: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(id="T-1", title="t", lifecycle_stage=stage, labels=labels or [])


def _rows() -> list[SkillContainerRow]:
    """One db row per hardcoded container — a registry mirroring the v0.1 seed."""
    return [
        SkillContainerRow(
            slug=c.id,
            name=c.name,
            recommended_roles=[c.recommended_role],
            supported_stages=list(c.supported_stages),
            required_input="",
            expected_output=c.expected_output,
            allowed_tools=[],
            default_write_mode=c.default_write_mode,
            risk_level=c.risk_level,
        )
        for c in reco.CONTAINERS
    ]


def test_db_registry_reproduces_the_hardcoded_recommendations() -> None:
    ticket = _ticket()
    hard = reco.recommend(ticket)
    via_db = reco.recommend(ticket, registry=DbRegistry(_rows(), []))
    assert [(r.skill_container, r.confidence, r.role, r.mapped_tool) for r in via_db] == [
        (r.skill_container, r.confidence, r.role, r.mapped_tool) for r in hard
    ]
    # confidence is categorical, never numeric — pin it explicitly
    assert all(
        r.confidence in (reco.CONFIDENCE_STRONG, reco.CONFIDENCE_PARTIAL, reco.CONFIDENCE_HEURISTIC)
        for r in via_db
    )


def test_an_active_personal_mapping_reflects_in_mapped_tool() -> None:
    ticket = _ticket()
    rows = _rows()
    target = reco.recommend(ticket)[0]  # the first strong recommendation
    target_row = next(r for r in rows if r.slug == target.skill_container)
    mapping = SkillMappingRow(
        container_id=target_row.id,
        scope="personal",
        provider="anthropic",
        connection="My Claude Code",
        status="active",
    )

    by_slug = {
        r.skill_container: r for r in reco.recommend(ticket, registry=DbRegistry(rows, [mapping]))
    }

    assert by_slug[target.skill_container].mapped_tool == "My Claude Code"  # the user's mapping
    # the other recommendations still carry the role's default tool hint
    others = [r for slug, r in by_slug.items() if slug != target.skill_container]
    assert others and all(r.mapped_tool != "My Claude Code" for r in others)


def test_a_disabled_mapping_does_not_override_the_role_hint() -> None:
    ticket = _ticket()
    rows = _rows()
    target = reco.recommend(ticket)[0]
    target_row = next(r for r in rows if r.slug == target.skill_container)
    disabled = SkillMappingRow(
        container_id=target_row.id,
        scope="personal",
        provider="anthropic",
        connection="My Claude Code",
        status="disabled",
    )

    by_slug = {
        r.skill_container: r for r in reco.recommend(ticket, registry=DbRegistry(rows, [disabled]))
    }

    assert by_slug[target.skill_container].mapped_tool == target.mapped_tool  # the hint stands
