"""Role/skill recommendations for a ticket (MOD-22 / Epic E17).

The ticket page (and an agent) should be able to ask "what should work on this,
and how?" and get **structured contracts** back — not prose. A recommendation
names an agent *role*, a *skill container* (the unit of work), the memory it
needs and what is *missing*, the expected output, the user's mapped tool, a
copy-paste MCP session snippet, and the risk/confidence/approval terms. The
contract is what makes a recommendation actionable and auditable rather than a
suggestion.

Like the lifecycle taxonomy (MOD-20) and the memory policies (MOD-21), v0.1 is
**hardcoded** (§8.9): each skill container is a frozen record and the registry is
a tuple — no DB table, no migration, no sync. The db-backed registry and the
personal/workspace skill *mappings* are v0.2 (FR-E17-2); ``mapped_tool`` here is
a descriptive label, not an executable binding (DEBT-06), and no secret is ever
handled (DEBT-07).

The rule engine is deliberately small and **keyed on the lifecycle stage**
(MOD-20 is the rule base: a stage already declares its recommended containers)
plus a few **signal** rules (ticket labels pull in a cross-stage container). Each
recommendation's ``missing_memory`` comes from the MOD-21 resolver, injected by
the caller so this module stays pure and testable without a database.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from kantaq_core import lifecycle, memory_policy

# Categorical confidence — no numeric scores in v0.1 (§8.9, MOD-22 "Behavior").
CONFIDENCE_STRONG: Final = "rule_match_strong"  # the stage explicitly recommends it
CONFIDENCE_PARTIAL: Final = "rule_match_partial"  # a signal (label) pulled it in
CONFIDENCE_HEURISTIC: Final = "heuristic_only"  # a fallback for a stage-less ticket

# Approval terms (E08: risky writes are always propose-first; reads are read-only).
APPROVAL_PROPOSE_FIRST: Final = "propose_first"
APPROVAL_READ_ONLY: Final = "read_only"

WRITE_PROPOSE: Final = "propose"
WRITE_READ: Final = "read"

# The MCP tools each container's role would use (MOD-09 catalog). Read containers
# read scoped context; propose containers also queue a proposal / comment.
_READ_TOOLS: Final = ("role_context_get", "ticket_get", "ticket_search", "memory_search")
_PROPOSE_TOOLS: Final = (*_READ_TOOLS, "ticket_comment_create", "agent_action_propose")

# v0.1 descriptive tool hint per role (DEBT-06: descriptive, not executable; the
# real personal/workspace mapping is the v0.2 db-backed skill_mappings).
_ROLE_TOOL_HINT: Final[dict[str, str]] = {
    "code_agent": "an MCP-connected coding agent (e.g. Claude Code, Codex)",
    "qa_agent": "an MCP-connected QA agent (e.g. a browser-driving agent)",
    "design_agent": "an MCP-connected design agent",
    "product_agent": "an MCP-connected product agent",
}


@dataclass(frozen=True)
class SkillContainer:
    """A unit of agent work (MOD-22 ``skill_containers``), hardcoded in v0.1."""

    id: str  # noqa: A003 — the registry key; matches a MOD-20 container slug
    name: str
    recommended_role: str  # one of the four MOD-21 agent roles
    supported_stages: tuple[str, ...]
    expected_output: str
    default_write_mode: str  # propose | read
    risk_level: str  # low | medium | high

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return _PROPOSE_TOOLS if self.default_write_mode == WRITE_PROPOSE else _READ_TOOLS

    @property
    def approval_rule(self) -> str:
        return (
            APPROVAL_PROPOSE_FIRST
            if self.default_write_mode == WRITE_PROPOSE
            else APPROVAL_READ_ONLY
        )

    @property
    def mapped_tool(self) -> str:
        return _ROLE_TOOL_HINT[self.recommended_role]


@dataclass(frozen=True)
class Recommendation:
    """The structured recommendation contract (FR-E17-1) for one (ticket, container)."""

    role: str
    skill_container: str
    why: str
    required_memory: tuple[str, ...]  # the role's policy scopes (MOD-21)
    missing_memory: tuple[str, ...]  # scopes the resolver found no entry for
    expected_output: str
    mapped_tool: str
    mcp_session_template: str
    risk_level: str
    confidence: str
    approval_rule: str


# The hardcoded registry: one SkillContainer per MOD-20 lifecycle container, so a
# stage's canonical containers all resolve to a role+contract. A test pins that
# every MOD-20 container slug has a record here (the E17 ripple gate — a new
# container cannot land without a role/contract decision), mirroring the
# memory-policy include-exclude completeness pin (MOD-21).
_C = SkillContainer
CONTAINERS: Final[tuple[SkillContainer, ...]] = (
    # intake
    _C(
        "triage",
        "Triage",
        "product_agent",
        ("intake",),
        "a triaged ticket: type, priority, and any duplicates flagged",
        WRITE_READ,
        "low",
    ),
    _C(
        "issue-shaping",
        "Issue shaping",
        "product_agent",
        ("intake",),
        "a shaped ticket: a clear problem statement and acceptance criteria",
        WRITE_PROPOSE,
        "low",
    ),
    _C(
        "duplicate-detection",
        "Duplicate detection",
        "product_agent",
        ("intake",),
        "a duplicate scan: links to similar existing tickets",
        WRITE_READ,
        "low",
    ),
    # discovery
    _C(
        "product-framing",
        "Product framing",
        "product_agent",
        ("discovery",),
        "a problem framing: the user need, scope, and success signals",
        WRITE_READ,
        "low",
    ),
    _C(
        "user-research",
        "User research",
        "product_agent",
        ("discovery",),
        "a research summary: who is affected and what they need",
        WRITE_READ,
        "low",
    ),
    _C(
        "requirement-synthesis",
        "Requirement synthesis",
        "product_agent",
        ("discovery",),
        "synthesised requirements and acceptance criteria",
        WRITE_PROPOSE,
        "low",
    ),
    # planning
    _C(
        "project-planning",
        "Project planning",
        "product_agent",
        ("planning",),
        "a plan: milestones, risks, and sequencing",
        WRITE_PROPOSE,
        "low",
    ),
    _C(
        "technical-decomposition",
        "Technical decomposition",
        "code_agent",
        ("planning",),
        "a technical breakdown into sub-tickets with dependencies",
        WRITE_PROPOSE,
        "medium",
    ),
    _C(
        "dependency-mapping",
        "Dependency mapping",
        "code_agent",
        ("planning",),
        "a dependency map across the affected components",
        WRITE_READ,
        "low",
    ),
    # design
    _C(
        "ux-design",
        "UX design",
        "design_agent",
        ("design",),
        "UX flows and states for the ticket",
        WRITE_PROPOSE,
        "low",
    ),
    _C(
        "design-system",
        "Design system",
        "design_agent",
        ("design",),
        "the design-system tokens and components that apply",
        WRITE_READ,
        "low",
    ),
    _C(
        "accessibility-review",
        "Accessibility review",
        "design_agent",
        ("design",),
        "an accessibility audit with WCAG findings",
        WRITE_READ,
        "low",
    ),
    _C(
        "design-review",
        "Design review",
        "design_agent",
        ("design",),
        "a design critique against the system and accessibility",
        WRITE_READ,
        "low",
    ),
    # implementation
    _C(
        "repo-investigation",
        "Repo investigation",
        "code_agent",
        ("implementation",),
        "a map of the code paths this change touches",
        WRITE_READ,
        "low",
    ),
    _C(
        "code-agent",
        "Code agent",
        "code_agent",
        ("implementation",),
        "a proposed code change, queued behind the approval gate",
        WRITE_PROPOSE,
        "high",
    ),
    _C(
        "test-generation",
        "Test generation",
        "code_agent",
        ("implementation",),
        "proposed tests covering the acceptance criteria",
        WRITE_PROPOSE,
        "medium",
    ),
    # review
    _C(
        "code-review",
        "Code review",
        "code_agent",
        ("review",),
        "a code review: correctness, security, and maintainability findings",
        WRITE_READ,
        "medium",
    ),
    _C(
        "security-review",
        "Security review",
        "code_agent",
        ("review",),
        "a security review: injection, authz, and data-exposure findings",
        WRITE_READ,
        "high",
    ),
    _C(
        "architecture-review",
        "Architecture review",
        "code_agent",
        ("review",),
        "an architecture review against the recorded decisions",
        WRITE_READ,
        "medium",
    ),
    # qa
    _C(
        "browser-qa",
        "Browser QA",
        "qa_agent",
        ("qa",),
        "a QA run: behaviours verified and regressions found",
        WRITE_READ,
        "medium",
    ),
    _C(
        "regression-testing",
        "Regression testing",
        "qa_agent",
        ("qa",),
        "a regression pass result against prior behaviour",
        WRITE_READ,
        "medium",
    ),
    _C(
        "bug-triage",
        "Bug triage",
        "qa_agent",
        ("qa",),
        "a triaged bug: severity, reproduction, and owner",
        WRITE_PROPOSE,
        "low",
    ),
    # release
    _C(
        "release-check",
        "Release check",
        "product_agent",
        ("release",),
        "a release-readiness checklist result",
        WRITE_READ,
        "medium",
    ),
    _C(
        "changelog",
        "Changelog",
        "product_agent",
        ("release",),
        "a proposed changelog entry",
        WRITE_PROPOSE,
        "low",
    ),
    _C(
        "docs-update",
        "Docs update",
        "product_agent",
        ("release",),
        "proposed documentation updates for the change",
        WRITE_PROPOSE,
        "low",
    ),
    _C(
        "deployment-check",
        "Deployment check",
        "code_agent",
        ("release",),
        "a deployment-readiness check (config, migrations, rollbacks)",
        WRITE_READ,
        "medium",
    ),
    # learn
    _C(
        "retrospective",
        "Retrospective",
        "product_agent",
        ("learn",),
        "a retrospective: what worked and what to change",
        WRITE_READ,
        "low",
    ),
    _C(
        "decision-log",
        "Decision log",
        "product_agent",
        ("learn",),
        "a captured decision with its rationale",
        WRITE_PROPOSE,
        "low",
    ),
    _C(
        "memory-curation",
        "Memory curation",
        "product_agent",
        ("learn",),
        "curated memory: entries to promote or retire",
        WRITE_PROPOSE,
        "low",
    ),
)

_BY_ID: Final[dict[str, SkillContainer]] = {c.id: c for c in CONTAINERS}

# Signal rules: a ticket label pulls in a cross-stage container (FR-E17-1
# "stage + signals"). Conservative on purpose — only unambiguous labels, and a
# signal recommendation is marked partial confidence (the stage drives strong).
_LABEL_SIGNALS: Final[dict[str, str]] = {
    "Security": "security-review",
    "Bug": "bug-triage",
    "QA/Testing": "browser-qa",
    "Frontend": "design-review",
    "Design": "ux-design",
    "Infrastructure": "deployment-check",
    "Documentation": "docs-update",
}

# A stage-less / legacy ticket still gets one safe, read-only default.
_FALLBACK_CONTAINER: Final = "repo-investigation"


class TicketLike(Protocol):
    """The fields :func:`recommend` reads — satisfied by the ORM ``Ticket`` and a stub."""

    id: str
    title: str
    lifecycle_stage: str
    labels: list[str]


def containers() -> tuple[SkillContainer, ...]:
    """Every hardcoded skill container (MOD-22 interface)."""
    return CONTAINERS


def container(slug: str) -> SkillContainer:
    """The container for a slug, or raise ``KeyError`` (strict, like lifecycle)."""
    return _BY_ID[slug]


def _session_template(role: str, ticket_id: str, container: SkillContainer) -> str:
    """A copy-paste MCP snippet to drive an agent on this ticket as ``role``.

    Descriptive (placeholders for port/token/grant — see ``docs/mcp.md``); v0.1
    does not mint or embed secrets (DEBT-07). The snippet pins the agent role,
    points at the loopback gateway, and names the first call + the allowed tools.
    """
    config = {
        "mcpServers": {
            "kantaq": {
                "type": "http",
                "url": "http://127.0.0.1:<port>/v1/mcp",
                "headers": {
                    "Authorization": "Bearer <member token>",
                    "mcp-grant-id": "<capability grant id>",
                    "mcp-agent-role": role,
                },
            }
        }
    }
    lines = [
        f"# kantaq MCP — drive a {role} on ticket {ticket_id} ({container.name})",
        "# 1. Add to your MCP client config (see docs/mcp.md for the token + grant):",
        json.dumps(config, indent=2),
        f'# 2. First call: role_context_get(ticket="{ticket_id}") to load the scoped bundle.',
        f"# 3. Then use: {', '.join(container.allowed_tools)}",
    ]
    return "\n".join(lines)


def _build(
    slug: str,
    ticket: TicketLike,
    *,
    confidence: str,
    why: str,
    missing_memory_for: Callable[[str], Sequence[str]] | None,
) -> Recommendation:
    spec = _BY_ID[slug]
    role = spec.recommended_role
    required = memory_policy.policy_for(role).include_scopes
    missing = tuple(missing_memory_for(role)) if missing_memory_for is not None else ()
    return Recommendation(
        role=role,
        skill_container=spec.id,
        why=why,
        required_memory=required,
        missing_memory=missing,
        expected_output=spec.expected_output,
        mapped_tool=spec.mapped_tool,
        mcp_session_template=_session_template(role, ticket.id, spec),
        risk_level=spec.risk_level,
        confidence=confidence,
        approval_rule=spec.approval_rule,
    )


def recommend(
    ticket: TicketLike,
    *,
    missing_memory_for: Callable[[str], Sequence[str]] | None = None,
) -> tuple[Recommendation, ...]:
    """Structured role/skill recommendations for a ticket (FR-E17-1).

    Rule engine: the ticket's lifecycle stage drives the strong recommendations
    (MOD-20's per-stage containers), then label signals add cross-stage
    containers at partial confidence. A ticket whose stage predates the taxonomy
    (a legacy row) falls back to one safe read-only default (heuristic).

    ``missing_memory_for(role)`` supplies the resolver's missing scopes per role
    (MOD-21); when omitted, ``missing_memory`` is empty (the pure default).
    """
    recs: list[Recommendation] = []
    seen: set[str] = set()
    stage = ticket.lifecycle_stage

    try:
        stage_containers = lifecycle.containers_for(stage)
        stage_title = next((s.title for s in lifecycle.stages() if s.slug == stage), stage)
    except lifecycle.UnknownStageError:
        stage_containers = ()
        stage_title = stage

    # Strong: the stage's canonical containers (only those we have a contract for).
    for slug in stage_containers:
        if slug in _BY_ID and slug not in seen:
            why = (
                f"At the {stage_title} stage, kantaq recommends a "
                f"{_BY_ID[slug].recommended_role} run {_BY_ID[slug].name}."
            )
            recs.append(
                _build(
                    slug,
                    ticket,
                    confidence=CONFIDENCE_STRONG,
                    why=why,
                    missing_memory_for=missing_memory_for,
                )
            )
            seen.add(slug)

    # Partial: label signals pull in a cross-stage container.
    for label in ticket.labels:
        signal_slug = _LABEL_SIGNALS.get(label)
        if signal_slug is not None and signal_slug not in seen:
            why = (
                f"The '{label}' label suggests {_BY_ID[signal_slug].name} "
                f"({_BY_ID[signal_slug].recommended_role})."
            )
            recs.append(
                _build(
                    signal_slug,
                    ticket,
                    confidence=CONFIDENCE_PARTIAL,
                    why=why,
                    missing_memory_for=missing_memory_for,
                )
            )
            seen.add(signal_slug)

    # Heuristic fallback: a stage-less/legacy ticket still gets one safe default.
    if not recs:
        why = "No lifecycle stage recognised; a read-only investigation is the safe default."
        recs.append(
            _build(
                _FALLBACK_CONTAINER,
                ticket,
                confidence=CONFIDENCE_HEURISTIC,
                why=why,
                missing_memory_for=missing_memory_for,
            )
        )

    return tuple(recs)


# --------------------------------------------------- light reco eval (E17-T3)
#
# A 30-fixture confusion matrix (FR-E17-3): for each (stage, labels) ticket, do
# the recommended *roles* match the hand-graded expectation? Scored role-wise
# over the four agent roles. The fixtures hardcode ``expected_roles`` (not
# computed from the engine) so a change to a container's role or a signal rule
# surfaces as false positives/negatives — the regression guard for E17.


class RecoFixtureError(ValueError):
    """The reco fixture file is malformed."""


@dataclass(frozen=True)
class RecoFixture:
    """One graded reco case: a ticket shape and the roles a human expects."""

    id: str  # noqa: A003
    title: str
    lifecycle_stage: str
    labels: tuple[str, ...]
    expected_roles: frozenset[str]


@dataclass(frozen=True)
class ConfusionMatrix:
    """Role-wise agreement of ``recommend`` with the graded expectations."""

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    fixtures: int

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 1.0

    @property
    def accuracy(self) -> float:
        total = (
            self.true_positives + self.false_positives + self.false_negatives + self.true_negatives
        )
        return (self.true_positives + self.true_negatives) / total if total else 1.0

    def render(self) -> str:
        return (
            f"reco confusion matrix over {self.fixtures} fixtures x "
            f"{len(memory_policy.ROLE_SLUGS)} roles: "
            f"TP={self.true_positives} FP={self.false_positives} "
            f"FN={self.false_negatives} TN={self.true_negatives} | "
            f"precision={self.precision:.3f} recall={self.recall:.3f} "
            f"accuracy={self.accuracy:.3f}"
        )


def reco_fixtures_path(base: Path | None = None) -> Path:
    """``<workspace-root>/evals/reco_fixtures.json`` (beside the context fixtures)."""
    from kantaq_core import evals

    return (base or evals.workspace_fixtures_dir().parent) / "reco_fixtures.json"


def load_reco_fixtures(path: Path | None = None) -> tuple[RecoFixture, ...]:
    """Load the 30 hand-graded reco fixtures."""
    path = path or reco_fixtures_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    fixtures: list[RecoFixture] = []
    for item in raw["fixtures"]:
        expected = frozenset(item["expected_roles"])
        unknown = expected - set(memory_policy.ROLE_SLUGS)
        if unknown:
            raise RecoFixtureError(f"{item['id']}: unknown expected_roles {sorted(unknown)}")
        fixtures.append(
            RecoFixture(
                id=item["id"],
                title=item["title"],
                lifecycle_stage=item["lifecycle_stage"],
                labels=tuple(item.get("labels", [])),
                expected_roles=expected,
            )
        )
    return tuple(fixtures)


def confusion_matrix(fixtures: Sequence[RecoFixture]) -> ConfusionMatrix:
    """Score ``recommend`` against the fixtures, role-wise (FR-E17-3)."""
    from types import SimpleNamespace

    tp = fp = fn = tn = 0
    for fx in fixtures:
        ticket = SimpleNamespace(
            id=fx.id, title=fx.title, lifecycle_stage=fx.lifecycle_stage, labels=list(fx.labels)
        )
        predicted = {rec.role for rec in recommend(ticket)}
        for role in memory_policy.ROLE_SLUGS:
            want = role in fx.expected_roles
            got = role in predicted
            if want and got:
                tp += 1
            elif got and not want:
                fp += 1
            elif want and not got:
                fn += 1
            else:
                tn += 1
    return ConfusionMatrix(tp, fp, fn, tn, len(fixtures))


__all__ = [
    "APPROVAL_PROPOSE_FIRST",
    "APPROVAL_READ_ONLY",
    "CONFIDENCE_HEURISTIC",
    "CONFIDENCE_PARTIAL",
    "CONFIDENCE_STRONG",
    "CONTAINERS",
    "ConfusionMatrix",
    "Recommendation",
    "RecoFixture",
    "RecoFixtureError",
    "SkillContainer",
    "TicketLike",
    "confusion_matrix",
    "container",
    "containers",
    "load_reco_fixtures",
    "recommend",
    "reco_fixtures_path",
]
