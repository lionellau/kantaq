"""The locked lifecycle taxonomy and its rules (MOD-20 / E14).

Pins the 9 stages (slugs, order, containers) against the MOD-20 table, the
recommended-next rule across every stage, and the Linear-status mapping the
importer (MOD-23) consumes. All pure — no engine, no session.
"""

from __future__ import annotations

import pytest

from kantaq_core import lifecycle

# The MOD-20 taxonomy table, transcribed — slug → (title, containers). A
# drift in code OR spec must show up as a failure here, not silently.
_TAXONOMY = {
    "intake": ("Intake", ("triage", "issue-shaping", "duplicate-detection")),
    "discovery": ("Discovery", ("product-framing", "user-research", "requirement-synthesis")),
    "planning": (
        "Planning",
        ("project-planning", "technical-decomposition", "dependency-mapping"),
    ),
    "design": (
        "Design",
        ("ux-design", "design-system", "accessibility-review", "design-review"),
    ),
    "implementation": ("Implementation", ("repo-investigation", "code-agent", "test-generation")),
    "review": ("Review", ("code-review", "security-review", "architecture-review")),
    "qa": ("QA", ("browser-qa", "regression-testing", "bug-triage")),
    "release": ("Release", ("release-check", "changelog", "docs-update", "deployment-check")),
    "learn": ("Learn", ("retrospective", "decision-log", "memory-curation")),
}


def test_taxonomy_is_the_locked_nine_in_canonical_order() -> None:
    assert lifecycle.STAGE_SLUGS == (
        "intake",
        "discovery",
        "planning",
        "design",
        "implementation",
        "review",
        "qa",
        "release",
        "learn",
    )
    assert lifecycle.stages() == lifecycle.STAGES
    assert len(lifecycle.STAGES) == 9


def test_every_stage_matches_the_mod20_table() -> None:
    for stage in lifecycle.stages():
        title, containers = _TAXONOMY[stage.slug]
        assert stage.title == title
        assert stage.containers == containers
        assert stage.purpose  # every stage explains itself
        assert lifecycle.containers_for(stage.slug) == containers


def test_membership_check_and_strict_helpers() -> None:
    assert lifecycle.is_stage("qa")
    assert not lifecycle.is_stage("build")  # a v0.0.5-era ad-hoc slug
    with pytest.raises(lifecycle.UnknownStageError, match="expected one of"):
        lifecycle.containers_for("build")
    with pytest.raises(lifecycle.UnknownStageError):
        lifecycle.recommend_next("shipping")


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("intake", ("discovery",)),
        ("discovery", ("planning",)),
        ("planning", ("design",)),
        ("design", ("implementation",)),
        ("implementation", ("review",)),
        ("review", ("qa", "implementation")),  # rework edge
        ("qa", ("release", "implementation")),  # rework edge
        ("release", ("learn",)),
        ("learn", ()),  # terminal
    ],
)
def test_recommend_next_follows_the_locked_rule(stage: str, expected: tuple[str, ...]) -> None:
    assert lifecycle.recommend_next(stage) == expected


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("intake", ("discovery",)),
        ("discovery", ("planning",)),
        ("planning", ("design",)),
        ("design", ("implementation",)),
        ("implementation", ("review",)),
        ("review", ("qa", "implementation")),
        ("qa", ("implementation",)),  # release withheld: open sub-tickets
        ("release", ("learn",)),  # already releasing — not gated
        ("learn", ()),
    ],
)
def test_open_subtickets_withhold_release_only(stage: str, expected: tuple[str, ...]) -> None:
    assert lifecycle.recommend_next(stage, has_open_subtickets=True) == expected


def test_linear_status_mapping_is_pinned() -> None:
    """The reference mapping for the JobWinAI Linear export (MOD-20 fixture).

    Exactly Linear's five statuses, each mapped into the locked taxonomy;
    terminal Linear statuses land at the terminal stage (done-ness lives in
    ``status``, not the stage).
    """
    assert dict(lifecycle.LINEAR_STATUS_TO_STAGE) == {
        "Backlog": "intake",
        "In Progress": "implementation",
        "In Review": "review",
        "Done": "learn",
        "Canceled": "learn",
    }
    assert all(lifecycle.is_stage(stage) for stage in lifecycle.LINEAR_STATUS_TO_STAGE.values())
    with pytest.raises(TypeError):
        lifecycle.LINEAR_STATUS_TO_STAGE["Backlog"] = "discovery"  # type: ignore[index]
