"""The project lifecycle taxonomy (MOD-20 / Epic E14).

The 9 default stages (PRD §7, locked in E14-T0) that drive role/skill
recommendations and agent handoff. Pure and hardcoded in v0.1: a stage is a
frozen record, the taxonomy is a tuple, and the helpers are functions of
their arguments — no I/O, no session. The ``Stage`` record leaves room for
db-backed config later (v0.3+) without schema churn.

Ordering is **advisory**: any stage may transition to any other (humans and
agents jump stages — a QA bounce back to ``implementation``, an import
landing at ``review``). Validity is taxonomy membership, enforced where
tickets are written (MOD-03); the order below exists to drive
``recommend_next``, not to gate writes.

This module is strict — an unknown slug raises ``UnknownStageError``. The
service boundary (MOD-03) is the tolerant layer: legacy rows whose stage
predates the locked taxonomy recommend nothing rather than erroring a feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

# The rework loop: inspection stages whose findings send work back to build.
_REWORK_TARGET: Final = "implementation"
_REWORK_SOURCES: Final = ("review", "qa")

# Withheld from recommendations while the ticket has open sub-tickets — you
# don't recommend releasing a parent whose children are still open. Computed
# from the parent/sub relation in v0.1; typed ``blocked-by`` relations join
# the same clause when E12-T3 lands.
_GATED_BY_OPEN_SUBTICKETS: Final = "release"


class UnknownStageError(ValueError):
    """The slug is not one of the 9 locked lifecycle stages."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"unknown lifecycle stage {stage!r}; expected one of {STAGE_SLUGS}")
        self.stage = stage


@dataclass(frozen=True)
class Stage:
    """One lifecycle stage: identity plus the labels the UI and MOD-22 render."""

    slug: str
    title: str
    purpose: str
    containers: tuple[str, ...]


# The locked taxonomy (PRD §7 verbatim; slugs are the lowercase names and the
# values ``tickets.lifecycle_stage`` may hold). Canonical order is the tuple
# order; tests pin slugs, order, and containers against the MOD-20 table.
STAGES: Final[tuple[Stage, ...]] = (
    Stage(
        slug="intake",
        title="Intake",
        purpose="Capture raw requests, ideas, bugs, and tasks",
        containers=("triage", "issue-shaping", "duplicate-detection"),
    ),
    Stage(
        slug="discovery",
        title="Discovery",
        purpose="Understand user need and problem framing",
        containers=("product-framing", "user-research", "requirement-synthesis"),
    ),
    Stage(
        slug="planning",
        title="Planning",
        purpose="Break work into milestones, tickets, and risks",
        containers=("project-planning", "technical-decomposition", "dependency-mapping"),
    ),
    Stage(
        slug="design",
        title="Design",
        purpose="Shape UX, UI, flows, and acceptance criteria",
        containers=("ux-design", "design-system", "accessibility-review", "design-review"),
    ),
    Stage(
        slug="implementation",
        title="Implementation",
        purpose="Build the actual change",
        containers=("repo-investigation", "code-agent", "test-generation"),
    ),
    Stage(
        slug="review",
        title="Review",
        purpose="Inspect quality, security, and maintainability",
        containers=("code-review", "security-review", "architecture-review"),
    ),
    Stage(
        slug="qa",
        title="QA",
        purpose="Verify behavior and regressions",
        containers=("browser-qa", "regression-testing", "bug-triage"),
    ),
    Stage(
        slug="release",
        title="Release",
        purpose="Prepare rollout and communication",
        containers=("release-check", "changelog", "docs-update", "deployment-check"),
    ),
    Stage(
        slug="learn",
        title="Learn",
        purpose="Capture decisions, outcomes, and reusable memory",
        containers=("retrospective", "decision-log", "memory-curation"),
    ),
)

STAGE_SLUGS: Final[tuple[str, ...]] = tuple(stage.slug for stage in STAGES)

_BY_SLUG: Final = {stage.slug: stage for stage in STAGES}
_ORDER: Final = {stage.slug: index for index, stage in enumerate(STAGES)}

# The reference Linear-status → lifecycle-stage mapping the importer (MOD-23)
# consumes, locked in MOD-20 against the JobWinAI Linear export fixture.
# Linear's one status axis maps onto two kantaq axes: ``status`` carries
# done-ness, the stage carries lifecycle position — so both terminal Linear
# statuses land at the terminal stage.
LINEAR_STATUS_TO_STAGE: Final = MappingProxyType(
    {
        "Backlog": "intake",
        "In Progress": "implementation",
        "In Review": "review",
        "Done": "learn",
        "Canceled": "learn",
    }
)


def stages() -> tuple[Stage, ...]:
    """The 9 stages in canonical order (MOD-20 interface)."""
    return STAGES


def is_stage(slug: str) -> bool:
    """Whether the slug is one of the locked taxonomy stages."""
    return slug in _BY_SLUG


def containers_for(stage: str) -> tuple[str, ...]:
    """The recommended skill containers for a stage (FR-E14-1)."""
    record = _BY_SLUG.get(stage)
    if record is None:
        raise UnknownStageError(stage)
    return record.containers


def recommend_next(stage: str, *, has_open_subtickets: bool = False) -> tuple[str, ...]:
    """The recommended next stages (FR-E14-2; rule locked in E14-T0).

    1. the next stage in canonical order (none for ``learn`` — terminal),
    2. plus the rework edge to ``implementation`` from ``review``/``qa``,
    3. minus ``release`` while the ticket has open sub-tickets.
    """
    if stage not in _BY_SLUG:
        raise UnknownStageError(stage)
    recommended: list[str] = []
    index = _ORDER[stage]
    if index + 1 < len(STAGES):
        recommended.append(STAGES[index + 1].slug)
    if stage in _REWORK_SOURCES:
        recommended.append(_REWORK_TARGET)
    if has_open_subtickets and _GATED_BY_OPEN_SUBTICKETS in recommended:
        recommended.remove(_GATED_BY_OPEN_SUBTICKETS)
    return tuple(recommended)
