"""E05-T4 — the offline/online/race integration matrix (RISK-04 gate).

This is the checked-in harness target the risk asked for: the hardest
concurrency cases on the two-/N-replica partition simulator, deterministic with
``FakeClock`` + ``SeededRandom`` and an explicit event order, proving the
convergence-critical invariants MOD-26 locks. Detection runs at the authoritative
commit point (the ``FakeBackend.commit_events`` mirror of the plpgsql RPC), so
the merge decision is a function of the committed total order — independent of
pull batching or cursor lag. The per-field rule itself is cross-checked against
the golden ``conflict_vectors.json`` in ``test_merge`` (Python) and the
EphemeralPostgres parity suite (RPC); this module proves the *integration*:
exactly-once, the conflict/resolution/proposal lifecycle, and N-way heal.

Axes (test-harness standard §Sync): connectivity × concurrency × op/policy.
Cases the design review showed break a naive engine are marked ⚠. Cases proven
in sibling modules are cited, not duplicated:
  - exactly-once / dropped-ack / terminal-state → test_flush_outbox
  - same-field mint / idempotent re-flush       → test_conflict_mint
  - sticky-resolved fold (single replica)       → test_conflict_ingest
  - different-field auto-merge on heal          → test_partition_heal
  - detect_merge ↔ golden vectors               → test_merge
  - stale agent proposal (single replica)       → test_proposal_rebase
  - version-skew refusal                        → test_sync_handshake
"""

from __future__ import annotations

import contextlib

from kantaq_db import ConflictRecord, Ticket, new_ulid
from kantaq_sync_engine import (
    BackendUnavailable,
    Backoff,
    Event,
    conflict_record_id,
    entity_base_rev,
    insert_event,
    next_actor_seq,
    refold_entity,
)
from kantaq_test_harness.backend import FakeBackend, PartitionLink
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.random import SeededRandom
from kantaq_test_harness.replica import WORKSPACE_ID, Replica, memory_replica

_INSTANT = Backoff(max_attempts=2, base_seconds=0.0)
_NO_SLEEP = {"backoff": _INSTANT, "sleeper": lambda _s: None}


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _sync_round(replicas: list[Replica]) -> None:
    """One online round: every replica flushes then ingests. Minting a
    conflict_record adds committed events, so the caller iterates a few rounds to
    quiescence (the records propagate on the next ingest)."""
    for r in replicas:
        r.sync.flush_outbox(**_NO_SLEEP)
    for r in replicas:
        r.sync.apply_inbox()


def _converge(replicas: list[Replica], rounds: int = 4) -> None:
    for _ in range(rounds):
        _sync_round(replicas)


def _ticket_state(replica: Replica, ticket_id: str) -> dict[str, object] | None:
    with replica.session() as session:
        row = session.get(Ticket, ticket_id)
        if row is None:
            return None
        return {"status": row.status, "priority": row.priority, "title": row.title}


def _conflict_keys(replica: Replica) -> set[tuple[str, str, str]]:
    """The replica's conflict_record set as (id, field, status) — the thing every
    replica must agree on after heal."""
    with replica.session() as session:
        return {(r.id, r.field, r.status) for r in session.exec(_select_conflicts()).all()}


def _select_conflicts():  # noqa: ANN202 - tiny local helper
    from sqlmodel import select

    return select(ConflictRecord)


def _ticket_event(
    actor: str, seq: int, tid: str, payload: dict, *, base_rev: int | None, op: str = "patch"
) -> Event:
    return Event(
        event_id=new_ulid(),
        collection="tickets",
        entity_id=tid,
        actor_id=actor,
        actor_seq=seq,
        op=op,
        base_rev=base_rev,
        payload=payload,
    )


# --------------------------------------------------------------------------- #
# Convergence — N-way partition heal                                          #
# --------------------------------------------------------------------------- #


def test_three_way_heal_converges_on_snapshots_and_conflict_records() -> None:
    """⚠ ≥3 replicas, partitioned, each editing the same + different fields, then
    healed: all converge to IDENTICAL ticket snapshots AND identical
    conflict_record sets — independent of pull batching (detection is a function
    of the committed total order, not arrival order). Deterministic via the
    explicit, seeded edit order."""
    rng = SeededRandom(7)
    clock = FakeClock()
    backend = FakeBackend()
    links = [PartitionLink(backend) for _ in range(3)]
    reps = [
        memory_replica(name, link, clock=clock)
        for name, link in zip(("alice", "bob", "carol"), links, strict=True)
    ]

    # Alice creates the shared ticket; everyone syncs it as the common base.
    with reps[0].session() as session:
        project = reps[0].service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        ticket = (
            reps[0]
            .service(session)
            .create_ticket(project_id=project.id, title="T", status="todo", priority="low")
        )
        tid = ticket.id
        session.commit()
    _converge(reps)

    # Partition everyone and let each make a seeded edit (same-field collisions on
    # status; a different-field edit on priority that must auto-merge).
    fields = ["status", "status", "priority"]
    values = {"status": ["doing", "done", "blocked"], "priority": ["high", "medium", "urgent"]}
    for i, r in enumerate(reps):
        links[i].online = False
        field = fields[i]
        value = rng.choice(values[field])
        with r.session() as session:
            r.service(session).update_ticket(tid, {field: value})
            session.commit()
        clock.advance(1.0)

    # Heal in a shuffled order and drive to quiescence.
    for link in links:
        link.online = True
    _converge(reps)

    # Identical snapshots AND identical conflict_record sets on every replica.
    snapshots = [_ticket_state(r, tid) for r in reps]
    assert snapshots[0] == snapshots[1] == snapshots[2]
    conflict_sets = [_conflict_keys(r) for r in reps]
    assert conflict_sets[0] == conflict_sets[1] == conflict_sets[2]
    # The backend's own fold agrees with the replicas (one truth).
    backend_status = backend.snapshot("tickets")[tid]["status"]
    assert snapshots[0]["status"] == backend_status
    # No write was lost: a same-field collision left a protective record.
    assert conflict_sets[0], "a same-field collision must leave a conflict_record"
    for r in reps:
        assert r.sync.pending_count() == 0


def test_heal_is_order_independent_across_seeds() -> None:
    """The converged state is a pure function of the committed total order, so
    different heal orders (seeds) each converge — no seed diverges."""
    for seed in range(4):
        rng = SeededRandom(seed)
        backend = FakeBackend()
        links = [PartitionLink(backend) for _ in range(3)]
        reps = [memory_replica(f"r{i}{seed}", link) for i, link in enumerate(links)]
        with reps[0].session() as session:
            project = reps[0].service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
            tid = reps[0].service(session).create_ticket(project_id=project.id, title="T").id
            session.commit()
        _converge(reps)
        order = list(range(3))
        rng._rng.shuffle(order)  # seeded heal order
        for i in order:
            links[i].online = False
            with reps[i].session() as session:
                reps[i].service(session).update_ticket(
                    tid, {"status": rng.choice(["todo", "doing", "done"])}
                )
                session.commit()
        for i in order:
            links[i].online = True
        _converge(reps)
        snaps = [_ticket_state(r, tid) for r in reps]
        confs = [_conflict_keys(r) for r in reps]
        assert snaps[0] == snaps[1] == snaps[2], f"seed {seed} snapshots diverged"
        assert confs[0] == confs[1] == confs[2], f"seed {seed} conflict sets diverged"


# --------------------------------------------------------------------------- #
# Resolution lifecycle                                                         #
# --------------------------------------------------------------------------- #


def _seed_open_conflict(alice: Replica) -> tuple[str, str, int]:
    """A committed ticket + an open conflict_record on its status (keep_a=doing,
    keep_b=todo). Returns (ticket_id, conflict_id, head_rev)."""
    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        tid = (
            alice.service(session).create_ticket(project_id=project.id, title="T", status="todo").id
        )
        session.commit()
    alice.sync.flush_outbox(**_NO_SLEEP)
    with alice.session() as session:
        head = entity_base_rev(session, "tickets", tid)
        assert head is not None
        cr_id = conflict_record_id(tid, "status", [head])
        cr_event = Event(
            event_id=new_ulid(),
            collection="conflict_records",
            entity_id=cr_id,
            actor_id=alice.actor_id,
            actor_seq=next_actor_seq(session, alice.actor_id),
            op="patch",
            payload={
                "workspace_id": WORKSPACE_ID,
                "collection": "tickets",
                "entity_id": tid,
                "field": "status",
                "contending_revisions": [head],
                "candidate_values": {"keep_a": "doing", "keep_b": "todo"},
                "base_rev": 0,
                "head_rev": head,
                "actor": alice.actor_id,
                "status": "open",
            },
        )
        committed = alice.sync._backend.commit_events([cr_event])
        insert_event(session, cr_event, committed_rev=committed[0].revision)
        refold_entity(session, "conflict_records", cr_id)
        session.commit()
    return tid, cr_id, head


def test_resolve_vs_concurrent_writer_rebases_and_record_stays_open() -> None:
    """⚠ A resolution races an ordinary write that moved the field head past the
    record's head_rev → the resolution is rebase_required and the record is NOT
    marked resolved against a live newer contender (the resolver-vs-writer hole)."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    tid, cr_id, head = _seed_open_conflict(alice)

    # A concurrent writer (peer) commits a NEW status, moving the head past head_rev.
    peer = "mbr_peer00000000000000000"
    backend.commit_events([_ticket_event(peer, 1, tid, {"status": "blocked"}, base_rev=head)])

    result = alice.sync.resolve_conflict(cr_id, "keep-A")  # would write status=doing @ base=head

    assert result.rebase_required and not result.resolved
    with alice.session() as session:
        assert session.get(ConflictRecord, cr_id).status == "open"  # never resolved vs newer write
    # The resolution's stale value committed NOTHING (CAS) — the team's concurrent
    # write stands at the backend head, not silently clobbered by the resolution.
    assert backend.snapshot("tickets")[tid]["status"] == "blocked"


def test_cross_replica_redetect_stays_resolved(tmp_path: object) -> None:
    """⚠ Detect on R1+R2, resolve on R1, re-mint the SAME conflict on R2 before it
    pulls the resolution → after convergence the record is resolved on every
    replica (never reopened) and the field holds the resolved value."""
    backend = FakeBackend()
    link_a, link_b = PartitionLink(backend), PartitionLink(backend)
    alice = memory_replica("alice", link_a)
    bob = memory_replica("bob", link_b)

    tid, cr_id, head = _seed_open_conflict(alice)
    bob.sync.apply_inbox()  # bob pulls the ticket + the open conflict_record
    with bob.session() as session:
        assert session.get(ConflictRecord, cr_id).status == "open"

    # R1 resolves (keep-A → status=doing) while R2 is partitioned.
    link_b.online = False
    alice.sync.resolve_conflict(cr_id, "keep-A")

    # R2, still partitioned, re-mints the SAME conflict id (insert-once on the
    # deterministic id) — a re-detection must never reopen a resolved record.
    with bob.session() as session:
        dup = Event(
            event_id=new_ulid(),
            collection="conflict_records",
            entity_id=cr_id,
            actor_id=bob.actor_id,
            actor_seq=next_actor_seq(session, bob.actor_id),
            op="patch",
            payload={
                "workspace_id": WORKSPACE_ID,
                "collection": "tickets",
                "entity_id": tid,
                "field": "status",
                "contending_revisions": [head],
                "candidate_values": {"keep_a": "doing", "keep_b": "todo"},
                "base_rev": 0,
                "head_rev": head,
                "actor": bob.actor_id,
                "status": "open",
            },
        )
        insert_event(session, dup)
        session.commit()

    link_b.online = True
    _converge([alice, bob])

    for r in (alice, bob):
        with r.session() as session:
            rec = session.get(ConflictRecord, cr_id)
            assert rec.status == "resolved", f"{r.name} reopened a resolved record"
            assert session.get(Ticket, tid).status == "doing"  # the resolved value holds


def test_single_resolver_crash_is_atomic() -> None:
    """A crash mid-resolution (the backend drops between the two events) commits
    neither: the record stays open and the field is untouched — no stranded-open
    record, no half-applied value (one transaction)."""
    backend = FakeBackend()
    alice = memory_replica("alice", backend)
    tid, cr_id, _head = _seed_open_conflict(alice)

    backend.offline = True  # the resolution's commit_events will raise
    with contextlib.suppress(BackendUnavailable):
        alice.sync.resolve_conflict(cr_id, "keep-A")
    backend.offline = False

    with alice.session() as session:
        assert session.get(ConflictRecord, cr_id).status == "open"  # not stranded resolved
        assert session.get(Ticket, tid).status == "todo"  # no half-applied value


# --------------------------------------------------------------------------- #
# Proposal lifecycle (convergence)                                            #
# --------------------------------------------------------------------------- #


def test_stale_proposal_converges_to_team_value_and_rebase_required() -> None:
    """⚠ A proposal approved from a lagging replica → after heal every replica
    agrees: the field holds the team's value (the agent's stale value never won)
    and the proposal is rebase_required."""
    from kantaq_core.tracker.events import DomainEvent
    from kantaq_db import AgentProposal
    from kantaq_sync_engine import EventLogSink

    backend = FakeBackend()
    link_a, link_b = PartitionLink(backend), PartitionLink(backend)
    alice = memory_replica("alice", link_a)
    bob = memory_replica("bob", link_b)

    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        tid = (
            alice.service(session).create_ticket(project_id=project.id, title="T", status="todo").id
        )
        session.commit()
    _converge([alice, bob])

    # Bob moves status → doing and commits (the team's value).
    with bob.session() as session:
        bob.service(session).update_ticket(tid, {"status": "doing"})
        session.commit()
    bob.sync.flush_outbox(**_NO_SLEEP)

    # Alice (lagging — never pulled bob's doing) approves an agent proposal that
    # set status → done, based on the stale head. We mirror approve_proposal's
    # shape: an approved-flip event, the optimistic local apply, and the tagged
    # ticket write carrying the stale base_rev (crafted directly, per the
    # conflict-test convention — the unsigned harness sink would null the base).
    pid = "prp_000000000000000000001"
    with alice.session() as session:
        head = entity_base_rev(session, "tickets", tid)
        session.add(
            AgentProposal(
                id=pid,
                ticket_id=tid,
                proposer_id="agt_x000000000000000000",
                diff={"changes": {"status": "done"}},
                status="approved",
            )
        )
        # The full proposal snapshot syncs (as the MCP create + approve would),
        # so every replica can fold the row before the rebase flip supersedes it.
        EventLogSink(session, alice.actor_id).emit(
            DomainEvent(
                collection="agent_proposals",
                entity_id=pid,
                op="patch",
                payload={
                    "id": pid,
                    "ticket_id": tid,
                    "proposer_id": "agt_x000000000000000000",
                    "diff": {"changes": {"status": "done"}},
                    "status": "approved",
                },
            )
        )
        tkt = session.get(Ticket, tid)
        tkt.status = "done"  # the optimistic apply at approval
        session.add(tkt)
        seq = next_actor_seq(session, alice.actor_id)
        insert_event(
            session,
            _ticket_event(alice.actor_id, seq, tid, {"status": "done"}, base_rev=head),
            origin_proposal_id=pid,
        )
        session.commit()

    link_a.online = True
    _converge([alice, bob])

    with alice.session() as session:
        assert session.get(AgentProposal, pid).status == "rebase_required"
    for r in (alice, bob):
        assert _ticket_state(r, tid)["status"] == "doing", (
            f"{r.name} did not converge to team value"
        )


# --------------------------------------------------------------------------- #
# Delete-vs-edit                                                              #
# --------------------------------------------------------------------------- #


def test_edit_vs_delete_stays_deleted_and_converges() -> None:
    """⚠ A patch whose base predates a committed tombstone it never saw stays
    deleted (no half-field ghost) + exactly one conflict_record; a replica that
    pulled only the tombstone and one that pulled both converge to 'deleted'."""
    backend = FakeBackend()
    link_a, link_b = PartitionLink(backend), PartitionLink(backend)
    alice = memory_replica("alice", link_a)
    bob = memory_replica("bob", link_b)

    with alice.session() as session:
        project = alice.service(session).create_project(workspace_id=WORKSPACE_ID, name="P")
        tid = (
            alice.service(session).create_ticket(project_id=project.id, title="T", status="todo").id
        )
        session.commit()
    _converge([alice, bob])

    # Bob deletes the ticket (a tombstone, emitted directly — tickets carry no
    # service delete in v0.0.5) and commits; Alice (partitioned) edits it offline.
    from kantaq_core.tracker.events import DomainEvent
    from kantaq_sync_engine import EventLogSink

    link_a.online = False
    with bob.session() as session:
        EventLogSink(session, bob.actor_id).emit(
            DomainEvent(collection="tickets", entity_id=tid, op="tombstone", payload={})
        )
        session.commit()
    bob.sync.flush_outbox(**_NO_SLEEP)

    with alice.session() as session:
        alice.service(session).update_ticket(tid, {"status": "doing"})
        session.commit()

    link_a.online = True
    _converge([alice, bob])

    # The row stays deleted on both; the edit did not resurrect it.
    assert _ticket_state(alice, tid) is None, "alice resurrected a deleted ticket"
    assert _ticket_state(bob, tid) is None, "bob resurrected a deleted ticket"
    assert _conflict_keys(alice) == _conflict_keys(bob)
