"""The locked memory-read policies and their enforcement (MOD-21 / E16-T1).

Pins the four agent policies against the MOD-21 table, proves the filter
partitions correctly with structured reasons, and — the SEC property
(NFR-E16-1) — proves a ``local`` entry is *never* returned to any agent role,
in any memory space, even one the role otherwise includes. Pure: no session,
expiry driven by FakeClock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kantaq_core import memory_policy as mp
from kantaq_core.memory.service import MEMORY_SPACES, REVIEW_STATUSES
from kantaq_db.models import MemoryEntry

# The MOD-21 policy table, transcribed — role → (include spaces, exclude spaces).
# A drift in code OR spec must surface as a failure here, not silently.
_POLICY_TABLE = {
    "code_agent": (
        ("codebase", "decision", "ticket", "project"),
        ("release", "workspace", "agent_run"),
    ),
    "qa_agent": (
        ("ticket", "release", "codebase", "decision"),
        ("workspace", "project", "agent_run"),
    ),
    "design_agent": (
        ("project", "ticket", "decision", "workspace"),
        ("codebase", "release", "agent_run"),
    ),
    "product_agent": (
        ("workspace", "project", "ticket", "decision", "release"),
        ("codebase", "agent_run"),
    ),
}

_NOW = datetime(2026, 6, 13, 12, 0, tzinfo=UTC).replace(tzinfo=None)


def _entry(
    space: str,
    *,
    visibility: str = "team",
    review_status: str = "approved",
    expires_at: datetime | None = None,
    ident: str | None = None,
) -> MemoryEntry:
    """A memory row with just the fields the policy reads (the real db model)."""
    return MemoryEntry(
        id=ident or f"mem-{space}-{visibility}-{review_status}",
        title=f"{space} note",
        space=space,
        visibility=visibility,
        review_status=review_status,
        expires_at=expires_at,
    )


# --------------------------------------------------------------------- the table


def test_role_slugs_are_the_four_locked_agent_roles() -> None:
    assert mp.ROLE_SLUGS == ("code_agent", "qa_agent", "design_agent", "product_agent")
    assert len(mp.POLICIES) == 4
    assert tuple(role.value for role in mp.AgentRole) == mp.ROLE_SLUGS


def test_every_policy_matches_the_mod21_table() -> None:
    for policy in mp.policies():
        include, exclude = _POLICY_TABLE[policy.applies_to_role.value]
        assert policy.include_scopes == include
        assert policy.exclude_scopes == exclude
        assert policy.rationale  # every policy explains itself (shown in previews)
        assert policy.policy_id.startswith(f"memory-policy/{policy.applies_to_role.value}/")
        assert policy.privacy_filter.min_visibility == "team"


def test_each_policy_partitions_all_seven_memory_spaces() -> None:
    # Single source of truth: the spaces a policy classifies are exactly MOD-19's
    # vocabulary, with no overlap — so a new space cannot land without a decision.
    for policy in mp.policies():
        classified = set(policy.include_scopes) | set(policy.exclude_scopes)
        assert classified == set(MEMORY_SPACES)
        assert not (set(policy.include_scopes) & set(policy.exclude_scopes))


def test_withheld_review_statuses_are_real_and_stale_rejected() -> None:
    for policy in mp.policies():
        assert policy.withheld_review_statuses <= set(REVIEW_STATUSES)
        assert policy.withheld_review_statuses == frozenset({"stale", "rejected"})


def test_policy_for_accepts_enum_and_str_and_fails_closed() -> None:
    assert mp.policy_for(mp.AgentRole.code_agent) is mp.policy_for("code_agent")
    assert mp.is_agent_role("qa_agent")
    assert not mp.is_agent_role("human_teammate")  # the eval baseline is not a policy
    with pytest.raises(mp.UnknownAgentRoleError) as excinfo:
        mp.policy_for("human_teammate")
    assert excinfo.value.role == "human_teammate"


def test_privacy_filter_admits_team_and_public_but_not_local() -> None:
    floor = mp.PrivacyFilter(min_visibility="team")
    assert floor.admits("team")
    assert floor.admits("public")
    assert not floor.admits("local")
    assert not floor.admits("nonsense")  # unknown visibility fails closed


# --------------------------------------------------------------------- filtering


def test_filter_partitions_with_structured_reasons_for_code_agent() -> None:
    policy = mp.policy_for("code_agent")
    entries = [
        _entry("codebase"),  # in scope → included
        _entry("decision"),  # in scope → included
        _entry("release"),  # explicit exclude → exclude_scope
        _entry("workspace"),  # explicit exclude → exclude_scope
        _entry("agent_run"),  # explicit exclude → exclude_scope
    ]
    result = mp.filter(entries, policy, now=_NOW)

    assert [e.space for e in result.included] == ["codebase", "decision"]
    reasons = {e.space: reason for e, reason in result.excluded}
    assert reasons == {
        "release": "exclude_scope:release",
        "workspace": "exclude_scope:workspace",
        "agent_run": "exclude_scope:agent_run",
    }
    # Every entry produces exactly one decision; counts reconcile.
    assert len(result.decisions) == len(entries)
    assert len(result.included) + len(result.excluded) == len(entries)
    assert [d.reason for d in result.decisions if d.included] == [
        "include_scope:codebase",
        "include_scope:decision",
    ]


def test_roles_differ_so_the_eval_has_signal() -> None:
    # A codebase note: in for code/qa, out for design/product.
    codebase = [_entry("codebase")]
    assert mp.filter(codebase, mp.policy_for("code_agent"), now=_NOW).included
    assert mp.filter(codebase, mp.policy_for("qa_agent"), now=_NOW).included
    assert not mp.filter(codebase, mp.policy_for("design_agent"), now=_NOW).included
    assert not mp.filter(codebase, mp.policy_for("product_agent"), now=_NOW).included
    # A release note: in for qa/product, out for code/design.
    release = [_entry("release")]
    assert mp.filter(release, mp.policy_for("qa_agent"), now=_NOW).included
    assert mp.filter(release, mp.policy_for("product_agent"), now=_NOW).included
    assert not mp.filter(release, mp.policy_for("code_agent"), now=_NOW).included
    assert not mp.filter(release, mp.policy_for("design_agent"), now=_NOW).included


# ------------------------------------------------------------------ NFR-E16-1


def test_local_entry_in_an_include_scope_is_still_excluded_for_privacy() -> None:
    # 'codebase' is in code_agent's include scopes — but a local entry there is
    # dropped for *privacy*, not scope. The reason names the privacy gate.
    policy = mp.policy_for("code_agent")
    local = _entry("codebase", visibility="local")
    result = mp.filter([local], policy, now=_NOW)
    assert result.included == ()
    assert result.excluded[0][1] == "privacy_filter:visibility_local"


def test_no_local_entry_is_ever_included_for_any_role_in_any_space() -> None:
    # The SEC property end to end: across every policy and every memory space, a
    # local entry never reaches `included`, and always for the privacy reason —
    # privacy is checked first, so scope/expiry/status can never override it.
    for policy in mp.policies():
        for space in MEMORY_SPACES:
            local = _entry(space, visibility="local")
            result = mp.filter([local], policy, now=_NOW)
            assert result.included == ()
            assert result.excluded[0][1] == "privacy_filter:visibility_local"


# ------------------------------------------------------------- expiry & status


def test_expired_entry_is_excluded_via_injected_clock() -> None:
    policy = mp.policy_for("code_agent")
    expired = _entry("codebase", expires_at=_NOW)  # expires exactly now → out
    live = _entry("codebase", expires_at=datetime(2030, 1, 1), ident="mem-live")
    result = mp.filter([expired, live], policy, now=_NOW)
    assert [e.id for e in result.included] == ["mem-live"]
    assert result.excluded[0] == (expired, "expired")


def test_stale_and_rejected_are_withheld_even_in_scope() -> None:
    policy = mp.policy_for("code_agent")
    assert (
        mp.filter([_entry("codebase", review_status="stale")], policy, now=_NOW).excluded[0][1]
        == "review_status:stale"
    )
    assert (
        mp.filter([_entry("codebase", review_status="rejected")], policy, now=_NOW).excluded[0][1]
        == "review_status:rejected"
    )
    # draft / proposed / approved in an include scope all pass the status gate.
    for status in ("draft", "proposed", "approved"):
        included = mp.filter([_entry("codebase", review_status=status)], policy, now=_NOW).included
        assert len(included) == 1


# ----------------------------------------------------------------- gate ordering


def test_decide_returns_the_first_failing_gate() -> None:
    policy = mp.policy_for("code_agent")
    # A local + expired + stale + excluded-scope entry fails everything; privacy
    # is first, so that is the reason — the SEC gate is never masked.
    worst = _entry("release", visibility="local", review_status="stale", expires_at=_NOW)
    assert mp.decide(policy, worst, now=_NOW).reason == "privacy_filter:visibility_local"
    # Team + expired + stale + excluded: expiry is next.
    expired = _entry("release", review_status="stale", expires_at=_NOW)
    assert mp.decide(policy, expired, now=_NOW).reason == "expired"
    # Team + live + stale + excluded: status before scope.
    stale = _entry("release", review_status="stale")
    assert mp.decide(policy, stale, now=_NOW).reason == "review_status:stale"


def test_out_of_scope_reason_for_an_unclassified_space() -> None:
    # Real policies classify every space, so exercise the branch with a policy
    # that deliberately leaves a space unlisted (neither included nor excluded).
    gapped = mp.MemoryPolicy(
        policy_id="memory-policy/test/gap",
        applies_to_role=mp.AgentRole.code_agent,
        include_scopes=("codebase",),
        exclude_scopes=("release",),
        privacy_filter=mp.PrivacyFilter(min_visibility="team"),
        rationale="test fixture with an unclassified space",
    )
    decision = mp.decide(gapped, _entry("ticket"), now=_NOW)
    assert decision.reason == "out_of_scope:ticket"
    assert not decision.included


def test_filter_over_an_empty_list_is_empty() -> None:
    result = mp.filter([], mp.policy_for("qa_agent"), now=_NOW)
    assert result.included == ()
    assert result.excluded == ()
    assert result.decisions == ()


# ----------------------------------------------------- promotion (E13-T4)


def test_promoted_team_approved_admitted_while_local_source_stays_excluded() -> None:
    """The promotion outcome is visible to agents; the local source never is.

    Copy-on-promote yields two rows in an include scope: the original ``local``
    (always excluded for privacy, NFR-E16-1) and the promoted ``team``
    ``approved`` copy (admitted). The pins above are unchanged — this asserts the
    promotion model lands on the right side of the locked policy."""
    policy = mp.policy_for("code_agent")  # 'codebase' is in scope
    local_source = _entry(
        "codebase", visibility="local", review_status="draft", ident="mem-local-source"
    )
    promoted_copy = _entry(
        "codebase", visibility="team", review_status="approved", ident="mem-team-copy"
    )
    result = mp.filter([local_source, promoted_copy], policy, now=_NOW)

    assert [e.id for e in result.included] == ["mem-team-copy"]
    reasons = {e.id: reason for e, reason in result.excluded}
    assert reasons == {"mem-local-source": "privacy_filter:visibility_local"}


def test_proposed_team_row_passes_the_status_gate() -> None:
    """A ``team`` ``proposed`` row (the intermediate promotion state) is admitted
    in an include scope — proposed is not a withheld status (only stale/rejected
    are), so the Inbox-context note is visible to the relevant agents."""
    policy = mp.policy_for("code_agent")
    proposed = _entry("codebase", visibility="team", review_status="proposed")
    assert len(mp.filter([proposed], policy, now=_NOW).included) == 1
