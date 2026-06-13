"""The role-aware context resolver (MOD-21 / E16-T2).

Two layers are tested: :func:`resolve` (pure, partition + reasons + missing +
token estimate), and the **precision/recall** of the rules-based resolver against
the hand-graded eval fixtures — the regression guard that the resolver still
agrees with the ground truth. A correct rules-based resolver scores 1.0/1.0,
because ``must_exclude`` only ever covers a policy-gate failure and an in-scope
but tangential entry is graded ``optional`` (unscored).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core import context, evals
from kantaq_core.memory.service import MemoryService
from kantaq_core.memory_policy import ROLE_SLUGS, UnknownAgentRoleError
from kantaq_db.models import Project, Ticket, Workspace
from kantaq_test_harness.clock import FakeClock

NOW = datetime(2026, 6, 1, tzinfo=UTC)
ACTOR = "mbr_actor000001"


def _mem(
    mem_id: str,
    *,
    space: str,
    visibility: str = "team",
    review_status: str = "approved",
    body: str = "",
) -> evals.EvalMemory:
    """A MemoryReadable fixture row (EvalMemory satisfies the protocol)."""
    return evals.EvalMemory(
        id=mem_id,
        title=mem_id,
        space=space,
        visibility=visibility,
        review_status=review_status,
        type="note",
        created_by="leo",
        body=body,
    )


# --------------------------------------------------------------- resolve (pure)


def test_resolve_rejects_non_agent_roles() -> None:
    """The human baseline and unknown roles are not resolver roles (fail closed)."""
    with pytest.raises(UnknownAgentRoleError):
        context.resolve("human_teammate", [], now=NOW)
    with pytest.raises(UnknownAgentRoleError):
        context.resolve("nonsense", [], now=NOW)


def test_resolve_partitions_with_reasons() -> None:
    candidates = [
        _mem("in-codebase", space="codebase"),
        _mem("out-release", space="release"),
        _mem("stale-decision", space="decision", review_status="stale"),
        _mem("local-codebase", space="codebase", visibility="local"),
    ]
    bundle = context.resolve("code_agent", candidates, now=NOW)

    assert {e.id for e in bundle.included} == {"in-codebase"}
    reasons = {e.entry_id: e.reason for e in bundle.excluded}
    assert reasons["out-release"] == "exclude_scope:release"
    assert reasons["stale-decision"] == "review_status:stale"
    assert reasons["local-codebase"] == "privacy_filter:visibility_local"
    assert bundle.policy_id == "memory-policy/code_agent/v1"
    assert bundle.rationale  # the policy's human-readable why, surfaced in preview


def test_resolve_reports_missing_expected_scopes() -> None:
    """`missing` lists the role's include_scopes that produced no included entry."""
    bundle = context.resolve("code_agent", [_mem("c", space="codebase")], now=NOW)
    # code_agent includes codebase, decision, ticket, project — only codebase filled.
    assert set(bundle.missing) == {"decision", "ticket", "project"}


@pytest.mark.parametrize("role", ROLE_SLUGS)
def test_resolve_never_includes_a_local_entry(role: str) -> None:
    """NFR-E16-1: across every policy, a local entry is never in `included`."""
    candidates = [
        _mem(f"local-{space}", space=space, visibility="local")
        for space in (
            "workspace",
            "project",
            "ticket",
            "codebase",
            "decision",
            "release",
            "agent_run",
        )
    ]
    bundle = context.resolve(role, candidates, now=NOW)
    assert bundle.included == ()
    assert all(e.reason == "privacy_filter:visibility_local" for e in bundle.excluded)


def test_estimate_tokens_is_ceil_of_chars_over_four() -> None:
    assert context.estimate_tokens("") == 0
    assert context.estimate_tokens("a") == 1  # ceil(1/4)
    assert context.estimate_tokens("aaaa") == 1
    assert context.estimate_tokens("aaaaa") == 2  # ceil(5/4)


def test_resolve_token_estimate_counts_ticket_and_included_text() -> None:
    bundle = context.resolve(
        "code_agent",
        [_mem("c", space="codebase", body="x" * 8)],  # title "c" (1) + body (8) = 9 chars
        now=NOW,
        extra_text="y" * 16,  # 16 chars
    )
    # 16 (ticket) + 1 (title) + 8 (body) = 25 chars -> ceil(25/4) = 7
    assert bundle.token_estimate == 7


# -------------------------------------------------- precision/recall (eval set)


def test_resolver_matches_the_graded_eval_fixtures() -> None:
    """The regression guard: resolver vs. hand-graded ground truth (RISK-02).

    For every (ticket, agent-role) cell, the resolver must include every
    ``must_include`` (recall) and exclude every ``must_exclude`` (precision);
    ``optional`` entries are unscored. Aggregated precision and recall are both
    1.0 for a correct rules-based resolver — the baseline a CI gate guards.
    """
    evalset = evals.load_eval_set()
    pool = evalset.memory

    tp = fp = fn = cells = 0
    for ticket in evalset.tickets:
        candidates = [pool[c.memory_id] for c in ticket.candidates]
        for role, expected in ticket.bundles.items():
            if role not in ROLE_SLUGS:  # skip the human_teammate baseline column
                continue
            cells += 1
            included = {e.id for e in context.resolve(role, candidates, now=NOW).included}
            must_in = set(expected.must_include)
            must_out = set(expected.must_exclude)

            false_pos = included & must_out
            false_neg = must_in - included
            assert not false_pos, f"{ticket.id}/{role}: includes must_exclude {sorted(false_pos)}"
            assert not false_neg, f"{ticket.id}/{role}: drops must_include {sorted(false_neg)}"

            tp += len(included & must_in)
            fp += len(false_pos)
            fn += len(false_neg)

    assert cells == len(evalset.tickets) * len(ROLE_SLUGS)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    assert precision == 1.0
    assert recall == 1.0


# ----------------------------------------------------- resolve_for_ticket (DB)


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def _ticket(session: Session) -> Ticket:
    ws = Workspace(name="W")
    session.add(ws)
    session.commit()
    project = Project(workspace_id=ws.id, name="P")
    session.add(project)
    session.commit()
    ticket = Ticket(project_id=project.id, title="T", description="ticket body")
    session.add(ticket)
    session.commit()
    return ticket


def test_resolve_for_ticket_gathers_linked_and_in_scope_team_only(session: Session) -> None:
    """Live gather: linked ∪ in-scope, team only; a local entry never appears."""
    clock = FakeClock()
    ticket = _ticket(session)
    svc = MemoryService(session, actor_id=ACTOR, source="mcp", now=clock.now)

    linked_codebase = svc.create_entry(title="arch", space="codebase", visibility="team")
    linked_release = svc.create_entry(title="ship", space="release", visibility="team")
    linked_local = svc.create_entry(title="secret", space="codebase", visibility="local")
    unlinked_codebase = svc.create_entry(title="conv", space="codebase", visibility="team")
    # An in-scope team entry in a space the policy excludes for code is never gathered
    svc.create_entry(title="ws-note", space="workspace", visibility="team")

    for entry in (linked_codebase, linked_release, linked_local):
        svc.link(entry.id, ticket.id, reason="r")

    bundle = context.resolve_for_ticket(
        session, ticket, "code_agent", actor_id=ACTOR, now=clock.now()
    )

    included = {e.id for e in bundle.included}
    assert included == {linked_codebase.id, unlinked_codebase.id}
    excluded = {e.entry_id: e.reason for e in bundle.excluded}
    # The linked release note is gathered (linked) but scoped out — visible reason.
    assert excluded.get(linked_release.id) == "exclude_scope:release"
    # The local entry is dropped at the gather seam: not included, not even excluded.
    assert linked_local.id not in included
    assert linked_local.id not in excluded
