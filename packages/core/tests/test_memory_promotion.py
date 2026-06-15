"""Memory promotion workflow: draft → proposed → approved (E13-T4 / MOD-19 §52).

The crux is copy-on-promote: promoting a ``local`` entry must NOT mutate its
``visibility`` (immutable + never-syncing, NFR-E13-1). So a local source is
copied into a NEW ``team`` ``proposed`` row and left untouched, while a ``team``
``{draft,stale}`` row transitions in place. Approve/reject are a human-only
compare-and-swap (proposer and approver are distinct actors, dogfood-gate #4).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.memory import (
    MemoryConflictError,
    MemoryService,
    MemoryValidationError,
    domain_visibility,
)
from kantaq_core.tracker import RecordingSink
from kantaq_db.models import AuditEvent
from kantaq_test_harness.clock import FakeClock

PROPOSER = "mbr_proposer01"
APPROVER = "mbr_approver01"


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def proposer(session: Session, clock: FakeClock, sink: RecordingSink) -> MemoryService:
    return MemoryService(session, actor_id=PROPOSER, source="app", sink=sink, now=clock.now)


@pytest.fixture
def approver(session: Session, clock: FakeClock, sink: RecordingSink) -> MemoryService:
    """A second service bound to a distinct human actor (dogfood-gate #4)."""
    return MemoryService(session, actor_id=APPROVER, source="app", sink=sink, now=clock.now)


def _audit_rows(session: Session, action: str) -> list[AuditEvent]:
    rows = session.exec(select(AuditEvent).where(AuditEvent.action == action)).all()
    return sorted(rows, key=lambda r: r.id)


def _entity_events(sink: RecordingSink, entity_id: str) -> list[str]:
    return [e.op for e in sink.events if e.entity_id == entity_id]


# ----------------------------------------------- promote a local source (copy)


def test_promote_local_copies_into_a_new_proposed_team_row(
    proposer: MemoryService, session: Session, sink: RecordingSink
) -> None:
    local = proposer.create_entry(
        title="private rationale",
        body="why we chose B",
        type="decision",
        space="project",
        confidence="high",
        linked_entities=["projects/abc"],
        visibility="local",
    )
    sink.events.clear()

    proposed = proposer.promote(local.id)

    # The NEW row is a distinct, team-visible, proposed copy of the content.
    assert proposed.id != local.id
    assert proposed.visibility == "team"
    assert proposed.review_status == "proposed"
    assert (proposed.title, proposed.body, proposed.type) == (
        "private rationale",
        "why we chose B",
        "decision",
    )
    assert proposed.space == "project"
    assert proposed.confidence == "high"
    assert proposed.linked_entities == ["projects/abc"]
    # Provenance notes the promotion lineage — but id-free, because this row
    # syncs and the local source's id must never leave the machine (NFR-E13-1).
    assert proposed.provenance["detail"] == "promoted from a local entry"
    assert local.id not in proposed.provenance["detail"]
    # domain_visibility now reads the proposal context (MOD-19 mapping table).
    assert domain_visibility(proposed.visibility, proposed.review_status, proposed.space) == (
        "proposal_context"
    )


def test_promote_local_leaves_the_source_immutable_and_silent(
    proposer: MemoryService, session: Session, sink: RecordingSink
) -> None:
    local = proposer.create_entry(title="secret", visibility="local")
    assert local.review_status == "draft"
    sink.events.clear()

    proposed = proposer.promote(local.id)

    # The original local row is untouched: visibility + review_status unchanged.
    refreshed = proposer.get_entry(local.id)
    assert refreshed.visibility == "local"
    assert refreshed.review_status == "draft"
    # NFR-E13-1 re-proven across promote: ZERO events for the local source.
    assert _entity_events(sink, local.id) == []
    # The new team row DID emit (the channel works).
    assert _entity_events(sink, proposed.id) == ["patch"]


def test_promote_local_aliases_no_mutable_columns(proposer: MemoryService) -> None:
    """The copied list is independent — mutating it must not touch the source."""
    local = proposer.create_entry(title="T", visibility="local", linked_entities=["projects/abc"])
    proposed = proposer.promote(local.id)
    proposed.linked_entities.append("releases/v1")
    assert proposer.get_entry(local.id).linked_entities == ["projects/abc"]


# ------------------------------------------------ promote a team row (in place)


@pytest.mark.parametrize("start_status", ["draft", "stale"])
def test_promote_team_draft_or_stale_transitions_in_place(
    proposer: MemoryService, sink: RecordingSink, start_status: str
) -> None:
    team = proposer.create_entry(title="shared", visibility="team")
    if start_status == "stale":
        proposer.update_entry(team.id, {"review_status": "stale"})
    sink.events.clear()

    proposed = proposer.promote(team.id)

    # Same row, flipped in place to proposed — no copy.
    assert proposed.id == team.id
    assert proposed.visibility == "team"
    assert proposed.review_status == "proposed"
    assert _entity_events(sink, team.id) == ["patch"]


@pytest.mark.parametrize("blocked", ["proposed", "approved", "rejected"])
def test_promote_team_already_decided_is_rejected(
    proposer: MemoryService, approver: MemoryService, blocked: str
) -> None:
    team = proposer.create_entry(title="shared", visibility="team")
    proposer.promote(team.id)  # → proposed
    if blocked == "approved":
        approver.approve(team.id)
    elif blocked == "rejected":
        approver.reject(team.id)
    with pytest.raises(MemoryValidationError, match="cannot be promoted"):
        proposer.promote(team.id)


def test_promote_missing_entry_is_not_found(proposer: MemoryService) -> None:
    from kantaq_core.memory import MemoryNotFoundError

    with pytest.raises(MemoryNotFoundError):
        proposer.promote("mem_missing")


# ------------------------------------------------------------- approve / reject


def test_approve_promotes_to_shared(proposer: MemoryService, approver: MemoryService) -> None:
    team = proposer.create_entry(title="shared", visibility="team", space="workspace")
    proposer.promote(team.id)

    approved = approver.approve(team.id)
    assert approved.review_status == "approved"
    assert approved.visibility == "team"
    assert domain_visibility(approved.visibility, approved.review_status, approved.space) == (
        "shared_workspace"
    )


def test_reject_declines_the_proposal(proposer: MemoryService, approver: MemoryService) -> None:
    team = proposer.create_entry(title="shared", visibility="team")
    proposer.promote(team.id)

    rejected = approver.reject(team.id)
    assert rejected.review_status == "rejected"


def test_approve_emits_and_audits_the_decision(
    proposer: MemoryService, approver: MemoryService, session: Session, sink: RecordingSink
) -> None:
    team = proposer.create_entry(title="shared", visibility="team")
    proposer.promote(team.id)
    sink.events.clear()

    approver.approve(team.id)

    assert _entity_events(sink, team.id) == ["patch"]
    approve_rows = _audit_rows(session, "memory.approve")
    assert len(approve_rows) == 1
    row = approve_rows[0]
    assert row.actor_id == APPROVER
    assert row.before is not None and row.before["review_status"] == "proposed"
    assert row.after is not None and row.after["review_status"] == "approved"


def test_proposer_and_approver_are_distinct_actors(
    proposer: MemoryService, approver: MemoryService, session: Session
) -> None:
    """Dogfood-gate #4: the propose and approve audit rows name different actors."""
    team = proposer.create_entry(title="shared", visibility="team")
    proposer.promote(team.id)
    approver.approve(team.id)

    promote_row = _audit_rows(session, "memory.promote")[0]
    approve_row = _audit_rows(session, "memory.approve")[0]
    assert promote_row.actor_id == PROPOSER
    assert approve_row.actor_id == APPROVER
    assert promote_row.actor_id != approve_row.actor_id


# ---------------------------------------------------------- compare-and-swap


def test_second_decision_on_a_decided_row_conflicts(
    proposer: MemoryService, approver: MemoryService
) -> None:
    """A CAS loser raises MemoryConflictError (the double-apply guard)."""
    team = proposer.create_entry(title="shared", visibility="team")
    proposer.promote(team.id)
    approver.approve(team.id)  # → approved (no longer proposed)

    with pytest.raises(MemoryConflictError, match="decided concurrently or is not proposed"):
        approver.approve(team.id)
    with pytest.raises(MemoryConflictError):
        approver.reject(team.id)


def test_approve_a_never_proposed_row_conflicts(approver: MemoryService) -> None:
    team = approver.create_entry(title="shared", visibility="team")  # stays draft
    with pytest.raises(MemoryConflictError):
        approver.approve(team.id)


# --------------------------------------------------------------------- audit


def test_promote_local_audits_both_rows_with_local_content_free(
    proposer: MemoryService, session: Session
) -> None:
    local = proposer.create_entry(title="secret plan", visibility="local")
    proposed = proposer.promote(local.id)

    promote_rows = _audit_rows(session, "memory.promote")
    assert len(promote_rows) == 2
    by_ref = {r.object_ref: r for r in promote_rows}
    team_row = by_ref[f"memory_entries/{proposed.id}"]
    local_row = by_ref[f"memory_entries/{local.id}"]
    # The team row carries a snapshot; the local source's audit is content-free.
    assert team_row.after is not None and team_row.after["review_status"] == "proposed"
    assert local_row.before is None and local_row.after is None


def test_promote_team_in_place_is_audited_with_snapshots(
    proposer: MemoryService, session: Session
) -> None:
    team = proposer.create_entry(title="shared", visibility="team")
    proposer.promote(team.id)
    rows = _audit_rows(session, "memory.promote")
    assert len(rows) == 1
    assert rows[0].before is not None and rows[0].before["review_status"] == "draft"
    assert rows[0].after is not None and rows[0].after["review_status"] == "proposed"
