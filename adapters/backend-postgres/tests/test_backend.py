"""E25-T1: the self-hosted ``BackendPort`` contract — CAS, dedup, round-trip.

Beyond the merge parity (``test_parity``) and the deny matrix (``test_deny``),
these pin the rest of the ``BackendPort`` the sync engine drives: the
compare-and-swap refusal (``RebaseRequired``, atomic), idempotent re-push (the
dedup floor), the push → pull → snapshot round-trip (commit order + LWW fold),
and the §B7 version handshake.
"""

from __future__ import annotations

import itertools

import pytest

from kantaq_backend_postgres import PostgresSyncBackend
from kantaq_protocol import Event
from kantaq_sync_engine.events import SYNC_VERSION as ENGINE_SYNC_VERSION
from kantaq_sync_engine.events import RebaseRequired

_seq = itertools.count(1)
_eid = itertools.count(1)
ENTITY = "tkt_backend000000000000001"
ACTOR = "mbr_aa0000000000000000000"


def _ev(*, op: str = "patch", base_rev: int | None = None, **payload: object) -> Event:
    return Event(
        event_id=f"e{next(_eid):025d}",
        collection="tickets",
        entity_id=ENTITY,
        actor_id=ACTOR,
        actor_seq=next(_seq),
        op=op,
        base_rev=base_rev,
        policy_ref=None,
        payload=dict(payload),
        sig=None,
    )


def test_session_init_advertises_the_server_versions(pg_backend: PostgresSyncBackend) -> None:
    init = pg_backend.session_init(sync_version=ENGINE_SYNC_VERSION, schema_version=1)
    assert init.sync_version == ENGINE_SYNC_VERSION
    assert init.schema_version >= 1


def test_idempotent_repush_is_a_duplicate_not_a_double_commit(
    pg_backend: PostgresSyncBackend,
) -> None:
    event = _ev(title="v1")
    first = pg_backend.commit_events([event], require_signature=False)[0]
    assert first.status == "committed"
    again = pg_backend.commit_events([event], require_signature=False)[0]
    assert again.status == "duplicate"
    assert again.revision == first.revision  # the prior commit's revision, no new row
    # only one row exists for this (actor, seq)
    assert len(pg_backend.pull("tickets")) == 1


def test_cas_refuses_a_contending_write_and_commits_nothing(
    pg_backend: PostgresSyncBackend,
) -> None:
    genesis = pg_backend.commit_events([_ev(status="todo")], require_signature=False)[0]
    # a second writer moves the field head
    pg_backend.commit_events(
        [_ev(base_rev=genesis.revision, status="doing")], require_signature=False
    )
    before = len(pg_backend.pull("tickets"))
    # a CAS write on the same field, still based on genesis → must refuse atomically
    with pytest.raises(RebaseRequired) as exc:
        pg_backend.commit_events(
            [_ev(base_rev=genesis.revision, status="done")], require_signature=False, cas=True
        )
    assert exc.value.conflicts  # carries the per-field contender detail
    assert len(pg_backend.pull("tickets")) == before  # nothing committed


def test_cas_allows_a_non_contending_write(pg_backend: PostgresSyncBackend) -> None:
    genesis = pg_backend.commit_events([_ev(status="todo")], require_signature=False)[0]
    pg_backend.commit_events(
        [_ev(base_rev=genesis.revision, status="doing")], require_signature=False
    )
    # a CAS write touching a DIFFERENT field does not contend → commits
    out = pg_backend.commit_events(
        [_ev(base_rev=genesis.revision, assignee="bob")], require_signature=False, cas=True
    )
    assert out[0].status == "committed"


def test_push_pull_snapshot_round_trip(pg_backend: PostgresSyncBackend) -> None:
    pg_backend.commit_events([_ev(title="A", status="todo")], require_signature=False)
    pulled = pg_backend.pull("tickets")
    assert [ce.revision for ce in pulled] == sorted(ce.revision for ce in pulled)  # commit order
    snap = pg_backend.snapshot("tickets")
    assert snap[ENTITY]["title"] == "A"
    assert snap[ENTITY]["status"] == "todo"
    # a tombstone removes the entity from the fold
    pg_backend.commit_events([_ev(op="tombstone")], require_signature=False)
    assert ENTITY not in pg_backend.snapshot("tickets")


def test_pull_since_cursor_only_returns_newer(pg_backend: PostgresSyncBackend) -> None:
    first = pg_backend.commit_events([_ev(title="A")], require_signature=False)[0]
    pg_backend.commit_events([_ev(title="B")], require_signature=False)
    newer = pg_backend.pull("tickets", since=first.revision)
    assert all(ce.revision > first.revision for ce in newer)
    assert len(newer) == 1


def test_ack_watermark_round_trips(pg_backend: PostgresSyncBackend) -> None:
    pg_backend.update_ack_watermark(member_id=ACTOR, replica_id="dev_1", acked_rev=5)
    pg_backend.update_ack_watermark(member_id=ACTOR, replica_id="dev_2", acked_rev=3)
    # the safe watermark is the MIN across live replicas (never prune above it)
    assert pg_backend.safe_watermark_rev() == 3
