"""Property test: the fold of emitted events equals the current ticket value.

The MOD-03 Domain-profile property (test-harness standard §4). For any sequence
of tracker mutations, replaying the ``DomainEvent`` stream the service emitted
must reproduce the ticket row exactly — this is what lets MOD-04 treat the
event log as the source of truth and the table as its fold.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlmodel import Session, SQLModel, create_engine

from kantaq_core import audit, lifecycle
from kantaq_core.tracker import RecordingSink, TrackerService, fold_entity
from kantaq_db.models import Workspace
from kantaq_test_harness.clock import FakeClock

ACTOR = "mbr_property001"

# One mutation = a dict of patchable-field changes the service will validate.
_mutation = st.fixed_dictionaries(
    {},
    optional={
        "title": st.text(
            alphabet=st.characters(codec="ascii", exclude_categories=("Cc", "Cs")),
            min_size=1,
            max_size=40,
        ).filter(lambda s: s.strip()),
        "status": st.sampled_from(["todo", "doing", "done"]),
        "priority": st.sampled_from(["low", "medium", "high", "urgent"]),
        "labels": st.lists(st.sampled_from(["bug", "ux", "infra", "docs"]), max_size=4),
        "description": st.text(max_size=80),
        "lifecycle_stage": st.sampled_from(lifecycle.STAGE_SLUGS),
        "acceptance_criteria": st.text(max_size=60),
    },
).filter(lambda d: d)


@settings(max_examples=40, deadline=None)
@given(mutations=st.lists(_mutation, min_size=0, max_size=8))
def test_fold_of_emitted_events_equals_current_ticket(
    mutations: list[dict[str, Any]],
) -> None:
    # Hypothesis reruns the body many times: build a fresh in-memory replica
    # per example (tmp_path-style fixtures would leak state across examples).
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    sink = RecordingSink()
    clock = FakeClock()
    with Session(engine) as session:
        workspace = Workspace(name="W")
        session.add(workspace)
        session.commit()
        service = TrackerService(session, actor_id=ACTOR, sink=sink, now=clock.now)
        project = service.create_project(workspace_id=workspace.id, name="P")
        ticket = service.create_ticket(project_id=project.id, title="Seed title")
        for changes in mutations:
            clock.advance(60)
            service.update_ticket(ticket.id, dict(changes))

        final = audit.snapshot(service.get_ticket(ticket.id))

    folded = fold_entity(ticket.id, [e for e in sink.events if e.collection == "tickets"])
    assert folded == final
