"""Role/skill recommendations (MOD-22 / Epic E17).

The contract is the product: a recommendation must name a valid agent role, a
real skill container, the memory it needs and what is missing, a categorical
confidence, and the approval terms — keyed on the lifecycle stage plus label
signals. These tests pin the contract shape, the stage/signal rules, the
container-coverage ripple gate, and the NFR that a recommended role is always one
the resolver actually serves.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kantaq_core import lifecycle, memory_policy, reco


def _ticket(stage: str = "implementation", labels: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(id="T-1", title="t", lifecycle_stage=stage, labels=labels or [])


# ------------------------------------------------------ the registry / ripple


def test_every_lifecycle_container_has_a_skill_contract() -> None:
    """The E17 ripple gate: no MOD-20 container without a role+contract here."""
    taxonomy = {slug for stage in lifecycle.stages() for slug in stage.containers}
    registry = {c.id for c in reco.CONTAINERS}
    assert taxonomy == registry  # exact: no missing, no orphan


def test_every_container_recommends_a_real_agent_role() -> None:
    for c in reco.CONTAINERS:
        assert c.recommended_role in memory_policy.ROLE_SLUGS, c.id
        assert c.default_write_mode in (reco.WRITE_PROPOSE, reco.WRITE_READ)
        assert c.risk_level in ("low", "medium", "high")


def test_container_lookup_is_strict() -> None:
    assert reco.container("code-review").name == "Code review"
    with pytest.raises(KeyError):
        reco.container("no-such-container")


# ------------------------------------------------------------- the rules


@pytest.mark.parametrize("stage", lifecycle.STAGE_SLUGS)
def test_each_stage_recommends_its_canonical_containers(stage: str) -> None:
    """Strong recommendations == the stage's MOD-20 containers, in order."""
    recs = reco.recommend(_ticket(stage=stage))
    strong = [r.skill_container for r in recs if r.confidence == reco.CONFIDENCE_STRONG]
    assert strong == list(lifecycle.containers_for(stage))
    assert all(r.role in memory_policy.ROLE_SLUGS for r in recs)


def test_a_label_signal_adds_a_cross_stage_container() -> None:
    """A Security label on a non-review ticket pulls in security-review (partial)."""
    recs = reco.recommend(_ticket(stage="implementation", labels=["Security"]))
    by_slug = {r.skill_container: r for r in recs}
    assert "security-review" in by_slug
    assert by_slug["security-review"].confidence == reco.CONFIDENCE_PARTIAL


def test_a_signal_does_not_duplicate_a_strong_recommendation() -> None:
    """At the review stage security-review is already strong; the label can't re-add it."""
    recs = reco.recommend(_ticket(stage="review", labels=["Security"]))
    security = [r for r in recs if r.skill_container == "security-review"]
    assert len(security) == 1
    assert security[0].confidence == reco.CONFIDENCE_STRONG


def test_a_legacy_stage_falls_back_to_one_safe_default() -> None:
    recs = reco.recommend(_ticket(stage="some-old-slug"))
    assert len(recs) == 1
    assert recs[0].skill_container == "repo-investigation"
    assert recs[0].confidence == reco.CONFIDENCE_HEURISTIC
    assert recs[0].approval_rule == reco.APPROVAL_READ_ONLY


# --------------------------------------------------------- the contract shape


def test_recommendation_contract_is_complete() -> None:
    rec = reco.recommend(_ticket(stage="implementation"))[0]
    # Every FR-E17-1 field is populated.
    assert rec.role and rec.skill_container and rec.why and rec.expected_output
    assert rec.required_memory == memory_policy.policy_for(rec.role).include_scopes
    assert rec.confidence in (
        reco.CONFIDENCE_STRONG,
        reco.CONFIDENCE_PARTIAL,
        reco.CONFIDENCE_HEURISTIC,
    )
    assert rec.approval_rule in (reco.APPROVAL_PROPOSE_FIRST, reco.APPROVAL_READ_ONLY)
    assert rec.risk_level in ("low", "medium", "high")
    assert "MCP-connected" in rec.mapped_tool


def test_propose_containers_are_propose_first_reads_are_read_only() -> None:
    recs = reco.recommend(_ticket(stage="implementation"))  # has code-agent (propose) + reads
    by_slug = {r.skill_container: r for r in recs}
    assert by_slug["code-agent"].approval_rule == reco.APPROVAL_PROPOSE_FIRST  # high-risk write
    assert by_slug["repo-investigation"].approval_rule == reco.APPROVAL_READ_ONLY


def test_session_template_pins_role_ticket_and_first_call() -> None:
    rec = reco.recommend(_ticket(stage="qa"))[0]
    tmpl = rec.mcp_session_template
    assert f'"mcp-agent-role": "{rec.role}"' in tmpl
    assert 'role_context_get(ticket="T-1")' in tmpl
    assert "127.0.0.1" in tmpl  # loopback only (MOD-18)
    assert "<member token>" in tmpl  # descriptive, no real secret (DEBT-07)


def test_missing_memory_is_injected_from_the_resolver() -> None:
    """missing_memory comes from the MOD-21 resolver, passed in by the caller."""
    seen: list[str] = []

    def missing_for(role: str) -> tuple[str, ...]:
        seen.append(role)
        return ("codebase", "decision")

    rec = reco.recommend(_ticket(stage="implementation"), missing_memory_for=missing_for)[0]
    assert rec.missing_memory == ("codebase", "decision")
    assert rec.role in seen
    # Pure default: no resolver, no missing memory.
    assert reco.recommend(_ticket(stage="implementation"))[0].missing_memory == ()


def test_confidence_is_categorical_never_numeric() -> None:
    """MOD-22: no numeric confidence scores in v0.1."""
    for stage in lifecycle.STAGE_SLUGS:
        for rec in reco.recommend(_ticket(stage=stage, labels=["Security", "Bug"])):
            assert isinstance(rec.confidence, str)
