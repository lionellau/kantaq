"""Policy-filtered memory search — the enforced read path (E13-T5 / MOD-19 + MOD-21).

``MemoryService.search`` is the single place search-time policy enforcement lives:
it narrows like ``list_entries`` and then applies the MOD-21 ``memory_policy.filter``
so **no entry the session's policy excludes is ever returned**. A human / unscoped
session (``policy=None``) reads unfiltered (the device owner's own memory, ``local``
notes included); an agent session passes its role policy and the privacy gate keeps
every ``local`` row out by construction (NFR-E16-1 / NFR-E13-1).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.memory import MEMORY_SPACES, MemoryService
from kantaq_core.memory_policy import ROLE_SLUGS, policy_for
from kantaq_core.tracker import RecordingSink
from kantaq_test_harness.clock import FakeClock

ACTOR = "mbr_searcher01"


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
def service(session: Session, clock: FakeClock) -> MemoryService:
    return MemoryService(session, actor_id=ACTOR, source="app", sink=RecordingSink(), now=clock.now)


# --------------------------------------------------- human / unscoped (no policy)


def test_policy_none_reads_unfiltered_including_local(service: MemoryService) -> None:
    """A human / unscoped session sees everything — team and its own local notes."""
    team = service.create_entry(title="team note", space="codebase")
    local = service.create_entry(title="private", space="codebase", visibility="local")

    result = service.search()

    assert {entry.id for entry in result.included} == {team.id, local.id}
    assert result.excluded == ()


def test_included_preserves_newest_first_order(service: MemoryService) -> None:
    first = service.create_entry(title="first", space="codebase")
    second = service.create_entry(title="second", space="codebase")
    # list_entries (and so search) returns newest-first by ULID id.
    assert [entry.id for entry in service.search().included] == [second.id, first.id]


# ------------------------------------------------------------- agent (policy set)


def test_agent_search_returns_only_in_scope_and_never_local(service: MemoryService) -> None:
    """code_agent: codebase is in scope, release is excluded, local is private,
    stale is withheld. Only the in-scope team entry comes back; each drop is
    reasoned."""
    policy = policy_for("code_agent")
    in_scope = service.create_entry(title="arch", space="codebase")
    local = service.create_entry(title="secret", space="codebase", visibility="local")
    out_scope = service.create_entry(title="release plan", space="release")
    stale = service.create_entry(title="aging", space="codebase")
    service.update_entry(stale.id, {"review_status": "stale"})

    result = service.search(policy=policy)

    assert {entry.id for entry in result.included} == {in_scope.id}
    # The local row is never present — the privacy gate is first and decisive.
    assert all(entry.id != local.id for entry in result.included)
    reasons = {entry.id: reason for entry, reason in result.excluded}
    assert reasons[local.id] == "privacy_filter:visibility_local"
    assert reasons[out_scope.id] == "exclude_scope:release"
    assert reasons[stale.id] == "review_status:stale"


def test_a_policy_excludes_what_another_includes(service: MemoryService) -> None:
    """The same entry is filtered differently by role: a ``release`` note is in
    scope for qa/product and out for code/design — the policy is the variable."""
    release_note = service.create_entry(title="rollback plan", space="release")
    # code_agent excludes release; product_agent includes it.
    code = service.search(policy=policy_for("code_agent"))
    product = service.search(policy=policy_for("product_agent"))
    assert release_note.id not in {entry.id for entry in code.included}
    assert release_note.id in {entry.id for entry in product.included}


def test_search_never_returns_a_local_entry_for_any_policy(service: MemoryService) -> None:
    """NFR-E16-1 / NFR-E13-1 at the search seam: one local entry in every space,
    no agent policy ever returns one."""
    local_ids = {
        service.create_entry(title=f"local-{space}", space=space, visibility="local").id
        for space in MEMORY_SPACES
    }
    for role in ROLE_SLUGS:
        result = service.search(policy=policy_for(role))
        assert all(entry.id not in local_ids for entry in result.included), role


def test_keyword_and_policy_compose(service: MemoryService) -> None:
    """The substring filter and the policy filter both apply; an out-of-scope
    match is still withheld."""
    policy = policy_for("code_agent")  # includes decision, excludes release
    keeper = service.create_entry(title="JWT decision", space="decision")
    service.create_entry(title="JWT release plan", space="release")
    service.create_entry(title="unrelated", space="decision")

    result = service.search(policy=policy, q="jwt")

    assert [entry.id for entry in result.included] == [keeper.id]


def test_expired_entries_excluded_under_the_clock(service: MemoryService, clock: FakeClock) -> None:
    """Expiry is deterministic on the service clock. By default expired rows are
    dropped before the policy sees them; with include_expired they reach the
    policy and drop as ``expired`` — never silently included."""
    policy = policy_for("code_agent")
    keeper = service.create_entry(title="keeper", space="codebase")
    fades = service.create_entry(title="fades", space="codebase", expires_at=clock.now())
    clock.advance(60)

    default = service.search(policy=policy)
    assert {entry.id for entry in default.included} == {keeper.id}

    with_expired = service.search(policy=policy, include_expired=True)
    assert {entry.id for entry in with_expired.included} == {keeper.id}
    reasons = {entry.id: reason for entry, reason in with_expired.excluded}
    assert reasons[fades.id] == "expired"


def test_space_filter_narrows_before_the_policy(service: MemoryService) -> None:
    """The structured ``space`` filter narrows first; the policy still applies."""
    policy = policy_for("product_agent")  # includes workspace + project
    ws = service.create_entry(title="ws note", space="workspace")
    service.create_entry(title="proj note", space="project")
    result = service.search(policy=policy, space="workspace")
    assert {entry.id for entry in result.included} == {ws.id}
