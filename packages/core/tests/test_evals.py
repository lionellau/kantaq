"""The context-eval fixture format, loader, and validator (MOD-21 / E16-T0).

Two jobs: prove the checked-in fixture set is valid and meets the Sprint-3
grading target, and prove the validator *catches* every way a hand-grader can
get a fixture wrong — including the SEC one (an agent bundle that includes a
local entry, or the human baseline including another actor's local entry).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from kantaq_core import evals

# --------------------------------------------------------- the checked-in set


def test_real_fixture_set_is_valid_and_meets_the_sprint3_target() -> None:
    report = evals.validate(evals.workspace_fixtures_dir())
    assert report.ok, report.problems
    assert report.ticket_count == 10
    assert report.graded_bundles == evals.SPRINT3_GRADED_TARGET == 50
    # All five columns graded for every ticket this sprint.
    assert report.per_role == dict.fromkeys(evals.EVAL_ROLES, 10)


def test_eval_roles_are_the_four_agents_plus_the_human_baseline() -> None:
    assert evals.EVAL_ROLES == (
        "code_agent",
        "qa_agent",
        "design_agent",
        "product_agent",
        "human_teammate",
    )
    assert evals.TARGET_TICKETS == 20
    assert evals.TARGET_BUNDLES == 100


def test_workspace_fixtures_dir_resolves_under_the_repo() -> None:
    base = evals.workspace_fixtures_dir()
    assert base.name == "fixtures"
    assert (base / "memory.json").is_file()
    assert (base / "tickets").is_dir()


def test_workspace_fixtures_dir_raises_when_no_workspace_above(tmp_path: Path) -> None:
    with pytest.raises(evals.EvalFixtureError):
        evals.workspace_fixtures_dir(tmp_path / "nowhere")


# ----------------------------------------------------------------- a tiny set


_POOL = [
    {
        "id": "m-team",
        "space": "codebase",
        "visibility": "team",
        "review_status": "approved",
        "type": "reference",
        "created_by": "alice",
        "title": "team note",
    },
    {
        "id": "m-own-local",
        "space": "ticket",
        "visibility": "local",
        "review_status": "draft",
        "type": "note",
        "created_by": "alice",
        "title": "alice's own local",
    },
    {
        "id": "m-foreign-local",
        "space": "ticket",
        "visibility": "local",
        "review_status": "draft",
        "type": "note",
        "created_by": "bob",
        "title": "bob's local",
    },
]


def _write_set(
    tmp_path: Path,
    *,
    pool: list[dict] | None = None,
    ticket: dict,
    baseline_owner: str = "alice",
) -> Path:
    base = tmp_path / "fixtures"
    (base / "tickets").mkdir(parents=True, exist_ok=True)
    (base / "memory.json").write_text(
        json.dumps({"baseline_owner": baseline_owner, "entries": pool or _POOL}),
        encoding="utf-8",
    )
    (base / "tickets" / f"{ticket['ticket']['id']}.json").write_text(
        json.dumps(ticket), encoding="utf-8"
    )
    return base


def _ticket(**bundle_overrides) -> dict:
    """A one-candidate ticket; bundles default to a clean code_agent grade."""
    bundles = {
        "code_agent": {
            "must_include": ["m-team"],
            "must_exclude": [],
            "optional": [],
            "rationale": "the only candidate is in scope",
        }
    }
    bundles.update(bundle_overrides)
    return {
        "ticket": {
            "id": "T-1",
            "title": "t",
            "lifecycle_stage": "implementation",
            "status": "open",
            "labels": [],
        },
        "candidate_memory": [{"id": "m-team", "linked": True, "link_reason": "r"}],
        "bundles": bundles,
    }


def test_minimal_valid_set_loads_and_validates(tmp_path: Path) -> None:
    base = _write_set(tmp_path, ticket=_ticket())
    evalset = evals.load_eval_set(base)
    assert evalset.baseline_owner == "alice"
    assert len(evalset.memory) == 3
    assert evalset.tickets[0].candidate_ids() == {"m-team"}
    assert evals.validate(base).ok


# ------------------------------------------------------------- pool problems


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("space", "nowhere", "unknown space"),
        ("visibility", "public", "unknown visibility"),
        ("review_status", "weird", "unknown review_status"),
        ("type", "essay", "unknown type"),
        ("created_by", "", "missing created_by"),
    ],
)
def test_pool_field_vocabularies_are_enforced(
    tmp_path: Path, field: str, value: str, needle: str
) -> None:
    pool = [dict(_POOL[0], **{field: value})]
    base = _write_set(tmp_path, pool=pool, ticket=_ticket())
    report = evals.validate(base)
    assert not report.ok
    assert any(needle in p for p in report.problems)


def test_duplicate_memory_id_raises(tmp_path: Path) -> None:
    base = _write_set(tmp_path, pool=[_POOL[0], _POOL[0]], ticket=_ticket())
    with pytest.raises(evals.EvalFixtureError, match="duplicate memory id"):
        evals.validate(base)


def test_missing_baseline_owner_raises(tmp_path: Path) -> None:
    base = tmp_path / "fixtures"
    (base / "tickets").mkdir(parents=True)
    (base / "memory.json").write_text(json.dumps({"entries": _POOL}), encoding="utf-8")
    with pytest.raises(evals.EvalFixtureError, match="baseline_owner"):
        evals.validate(base)


# ----------------------------------------------------------- ticket problems


def test_unknown_lifecycle_stage_is_caught(tmp_path: Path) -> None:
    ticket = _ticket()
    ticket["ticket"]["lifecycle_stage"] = "wishing"
    base = _write_set(tmp_path, ticket=ticket)
    assert any("unknown lifecycle_stage" in p for p in evals.validate(base).problems)


def test_candidate_not_in_pool_is_caught(tmp_path: Path) -> None:
    ticket = _ticket()
    ticket["candidate_memory"].append({"id": "m-ghost", "linked": False})
    ticket["bundles"]["code_agent"]["must_exclude"] = ["m-ghost"]
    base = _write_set(tmp_path, ticket=ticket)
    assert any("not in the pool" in p for p in evals.validate(base).problems)


def test_duplicate_candidate_is_caught(tmp_path: Path) -> None:
    ticket = _ticket()
    ticket["candidate_memory"].append({"id": "m-team", "linked": False})
    base = _write_set(tmp_path, ticket=ticket)
    assert any("duplicate candidate" in p for p in evals.validate(base).problems)


def test_linked_candidate_needs_a_reason(tmp_path: Path) -> None:
    ticket = _ticket()
    ticket["candidate_memory"] = [{"id": "m-team", "linked": True, "link_reason": "  "}]
    base = _write_set(tmp_path, ticket=ticket)
    assert any("needs a reason" in p for p in evals.validate(base).problems)


# ----------------------------------------------------------- bundle problems


def test_bundle_must_partition_every_candidate(tmp_path: Path) -> None:
    # Two candidates but the bundle grades only one → the other is ungraded.
    ticket = _ticket()
    ticket["candidate_memory"].append({"id": "m-own-local", "linked": False})
    base = _write_set(tmp_path, ticket=ticket)
    assert any("leaves candidates ungraded" in p for p in evals.validate(base).problems)


def test_bundle_buckets_must_be_disjoint(tmp_path: Path) -> None:
    ticket = _ticket(
        code_agent={
            "must_include": ["m-team"],
            "must_exclude": ["m-team"],
            "optional": [],
            "rationale": "contradiction",
        }
    )
    assert any("overlap" in p for p in evals.validate(_write_set(tmp_path, ticket=ticket)).problems)


def test_bundle_cannot_grade_a_non_candidate(tmp_path: Path) -> None:
    ticket = _ticket(
        code_agent={
            "must_include": ["m-team", "m-own-local"],
            "must_exclude": [],
            "optional": [],
            "rationale": "grades something not offered",
        }
    )
    assert any(
        "non-candidate" in p for p in evals.validate(_write_set(tmp_path, ticket=ticket)).problems
    )


def test_unknown_role_is_caught(tmp_path: Path) -> None:
    ticket = _ticket(
        researcher_agent={
            "must_include": ["m-team"],
            "must_exclude": [],
            "optional": [],
            "rationale": "not a real role",
        }
    )
    assert any(
        "not one of the five eval roles" in p
        for p in evals.validate(_write_set(tmp_path, ticket=ticket)).problems
    )


def test_missing_rationale_is_caught(tmp_path: Path) -> None:
    ticket = _ticket(
        code_agent={"must_include": ["m-team"], "must_exclude": [], "optional": [], "rationale": ""}
    )
    assert any(
        "missing rationale" in p
        for p in evals.validate(_write_set(tmp_path, ticket=ticket)).problems
    )


# --------------------------------------------------------- the SEC invariant


def test_agent_bundle_including_a_local_entry_is_rejected(tmp_path: Path) -> None:
    # NFR-E16-1 at the fixture layer: an agent must never be graded to include a
    # local entry, even its own-actor's.
    ticket = _ticket()
    ticket["candidate_memory"] = [
        {"id": "m-team", "linked": False},
        {"id": "m-own-local", "linked": False},
    ]
    ticket["bundles"]["code_agent"] = {
        "must_include": ["m-team", "m-own-local"],
        "must_exclude": [],
        "optional": [],
        "rationale": "wrongly includes a local entry",
    }
    problems = evals.validate(_write_set(tmp_path, ticket=ticket)).problems
    assert any("NFR-E16-1" in p for p in problems)


def test_human_baseline_may_include_own_local_but_not_a_foreign_local(tmp_path: Path) -> None:
    candidates = [
        {"id": "m-team", "linked": False},
        {"id": "m-own-local", "linked": False},
        {"id": "m-foreign-local", "linked": False},
    ]
    # Valid: the human owner includes their own local, excludes the foreign one.
    good = _ticket(
        human_teammate={
            "must_include": ["m-team", "m-own-local"],
            "must_exclude": ["m-foreign-local"],
            "optional": [],
            "rationale": "owner sees own local, not bob's",
        }
    )
    good["candidate_memory"] = candidates
    good["bundles"]["code_agent"] = {
        "must_include": ["m-team"],
        "must_exclude": ["m-own-local", "m-foreign-local"],
        "optional": [],
        "rationale": "agent sees neither local",
    }
    assert evals.validate(_write_set(tmp_path, ticket=good)).ok

    # Invalid: the human baseline includes another actor's local entry.
    bad = _ticket(
        human_teammate={
            "must_include": ["m-team", "m-foreign-local"],
            "must_exclude": ["m-own-local"],
            "optional": [],
            "rationale": "wrongly includes bob's local",
        }
    )
    bad["candidate_memory"] = candidates
    bad["bundles"]["code_agent"] = {
        "must_include": ["m-team"],
        "must_exclude": ["m-own-local", "m-foreign-local"],
        "optional": [],
        "rationale": "agent sees neither local",
    }
    problems = evals.validate(_write_set(tmp_path, ticket=bad)).problems
    assert any("another actor's local entry" in p for p in problems)


# ------------------------------------------------------------------- parsing


def test_parse_expires_accepts_iso_or_null_and_rejects_others() -> None:
    assert evals._parse_expires(None) is None
    assert evals._parse_expires("2026-06-13T00:00:00") == datetime(2026, 6, 13)
    with pytest.raises(evals.EvalFixtureError):
        evals._parse_expires(12345)
