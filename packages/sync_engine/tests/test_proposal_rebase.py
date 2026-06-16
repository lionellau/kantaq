"""E05-T3: a stale agent proposal is bounced to ``rebase_required`` on commit.

When a human approves an agent proposal whose ticket write turns out to be based
on a revision the team has moved past, the agent's stale value must not silently
land (the §8.5 propose-first rule, MOD-26 §B3). The proposal's ticket write
(tagged ``origin_proposal_id``) is committed as a compare-and-swap (``cas=True``):
a genuine field clash makes the RPC commit nothing and raise ``RebaseRequired``,
so the proposal flips to ``rebase_required`` and the agent's value never lands —
the intervening commit stands. The proposal's diff is preserved for re-decision.

Conflict mode requires the signed/``base_rev`` path (MOD-26 §B6); the unsigned
harness sink nulls base_rev, so the proposal's ticket write is crafted directly
with the stale base it was made against (the conflict-test convention).
"""

from __future__ import annotations

from kantaq_db import AgentProposal, AuditEvent, Ticket, new_ulid
from kantaq_sync_engine import (
    PROPOSAL_POLICY_STRICT_REBASE,
    Event,
    entity_base_rev,
    insert_event,
    mark_proposal_origin,
    next_actor_seq,
)
from kantaq_test_harness.backend import FakeBackend, PartitionLink
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, memory_replica

PID = "prp_000000000000000000001"


def _ticket_event(actor: str, seq: int, tid: str, payload: dict, *, base_rev: int | None) -> Event:
    return Event(
        event_id=new_ulid(),
        collection="tickets",
        entity_id=tid,
        actor_id=actor,
        actor_seq=seq,
        op="patch",
        base_rev=base_rev,
        payload=payload,
    )


def _approve_stale_proposal(alice: Replica, tid: str, diff: dict, base_rev: int) -> None:
    """Mirror approve_proposal on a lagging replica: an approved proposal row, the
    optimistic local apply, and the tagged ticket write carrying the stale base."""
    with alice.session() as session:
        session.add(
            AgentProposal(
                id=PID,
                ticket_id=tid,
                proposer_id="agt_x000000000000000000",
                diff={"changes": diff},
                status="approved",
            )
        )
        tkt = session.get(Ticket, tid)
        for field, value in diff.items():
            setattr(tkt, field, value)  # the optimistic apply at approval
        session.add(tkt)
        seq = next_actor_seq(session, alice.actor_id)
        insert_event(
            session, _ticket_event(alice.actor_id, seq, tid, dict(diff), base_rev=base_rev)
        )
        mark_proposal_origin(session, alice.actor_id, seq, PID)
        session.commit()


def _setup(team_change: dict) -> tuple[Replica, Replica, str, int]:
    """alice creates a ticket (both converge); bob makes ``team_change`` and
    commits it; alice stays lagging. Returns (alice, bob, ticket_id, base_rev)
    where base_rev is the head the proposal is made against (pre-team-change)."""
    backend = FakeBackend()
    alice = memory_replica("alice", PartitionLink(backend))
    bob = memory_replica("bob", PartitionLink(backend))
    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        tid = (
            alice.service(session)
            .create_ticket(project_id=project.id, title="T", status="todo", priority="low")
            .id
        )
        session.commit()
    alice.sync.flush_outbox()
    bob.sync.apply_inbox()
    with alice.session() as session:
        base = entity_base_rev(session, "tickets", tid)
        assert base is not None
    with bob.session() as session:
        bob.service(session).update_ticket(tid, team_change)
        session.commit()
    bob.sync.flush_outbox()  # the team's value commits; alice has NOT pulled it
    return alice, bob, tid, base


def test_stale_proposal_bounces_and_team_value_stands() -> None:
    alice, bob, tid, base = _setup({"status": "doing"})
    _approve_stale_proposal(alice, tid, {"status": "done"}, base)

    flush = alice.sync.flush_outbox()  # auto_rebase (default)

    assert flush.rebased == 1
    assert flush.minted == 0  # a proposal write never mints an ordinary conflict_record
    alice.sync.apply_inbox()  # pull the team's committed value
    with alice.session() as session:
        proposal = session.get(AgentProposal, PID)
        assert proposal.status == "rebase_required"
        assert proposal.diff == {"changes": {"status": "done"}}  # preserved for re-apply
        # The agent's stale 'done' never landed — the team's 'doing' stands.
        assert session.get(Ticket, tid).status == "doing"
        actions = [a.action for a in session.exec(_audits()).all()]
        assert "proposal.rebase_required" in actions
    # The backend never recorded the agent's stale value.
    assert alice.sync._backend.snapshot("tickets")[tid]["status"] == "doing"


def test_nonconflicting_stale_proposal_auto_merges_under_auto_rebase() -> None:
    """A proposal touching a DIFFERENT field than the team's edit does not bounce
    under auto_rebase — its CAS commits (auto-merge), so the human is never nagged."""
    alice, bob, tid, base = _setup({"status": "doing"})
    _approve_stale_proposal(alice, tid, {"priority": "high"}, base)

    flush = alice.sync.flush_outbox()

    assert flush.rebased == 0  # different field — clean auto-merge, no bounce
    alice.sync.apply_inbox()
    with alice.session() as session:
        assert session.get(AgentProposal, PID).status == "approved"
        ticket = session.get(Ticket, tid)
        assert ticket.priority == "high"  # the proposal applied
        assert ticket.status == "doing"  # the team's edit stands


def test_strict_rebase_flags_a_nonconflicting_stale_proposal() -> None:
    """strict_rebase re-confirms any proposal that raced a change: the CAS commits
    the non-conflicting write but the proposal is flagged for re-confirmation."""
    alice, bob, tid, base = _setup({"status": "doing"})
    _approve_stale_proposal(alice, tid, {"priority": "high"}, base)

    flush = alice.sync.flush_outbox(proposal_stale_policy=PROPOSAL_POLICY_STRICT_REBASE)

    assert flush.rebased == 1  # strict: any stale base re-confirms
    with alice.session() as session:
        assert session.get(AgentProposal, PID).status == "rebase_required"
        assert session.get(Ticket, tid).priority == "high"  # value applied (it never conflicted)


def test_approve_proposal_tags_the_ticket_write_proposal_originated() -> None:
    """approve_proposal marks its ticket write with origin_proposal_id so the
    flush seam can tell it apart from an ordinary human edit (MOD-26 §B3)."""
    from sqlmodel import select

    from kantaq_core import proposals
    from kantaq_db import EventLog

    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        tid = (
            alice.service(session).create_ticket(project_id=project.id, title="T", status="todo").id
        )
        session.add(
            AgentProposal(
                id=PID,
                ticket_id=tid,
                proposer_id="agt_x000000000000000000",
                diff={"changes": {"status": "doing"}},
                status="pending",
            )
        )
        session.commit()
    with alice.session() as session:
        proposals.approve_proposal(session, PID, actor_id=alice.actor_id, source="app")
        session.commit()
    with alice.session() as session:
        tagged = session.exec(
            select(EventLog)
            .where(EventLog.collection == "tickets")
            .where(EventLog.origin_proposal_id == PID)
        ).all()
        assert len(tagged) == 1
        assert tagged[0].entity_id == tid


def _audits():  # noqa: ANN202 - tiny local helper
    from sqlmodel import select

    return select(AuditEvent)
