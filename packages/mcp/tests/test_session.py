"""Session derivation, expiry, and rate counters (E09-T2, FR-E09-1/4)."""

from __future__ import annotations

from datetime import timedelta

from kantaq_core.identity import VerifiedActor
from kantaq_mcp.session import (
    RATE_LIMIT_PER_MINUTE,
    RATE_LIMIT_PER_SESSION,
    WRITE_MODE_PROPOSE_ONLY,
    WRITE_MODE_READ_ONLY,
    SessionRegistry,
    derive_session,
)
from kantaq_test_harness.clock import FakeClock


def _actor(role: str, scopes: tuple[str, ...] = ()) -> VerifiedActor:
    return VerifiedActor(member_id="m-1", role=role, token_id="t-1", scopes=scopes)


def test_owner_session_gets_both_tools_propose_only() -> None:
    session = derive_session(_actor("Owner"), session_id="s", now=FakeClock().now())
    assert session.allowed_tools == ("ticket_get", "agent_action_propose")
    assert session.write_mode == WRITE_MODE_PROPOSE_ONLY  # nothing direct-writes in v0.0.5


def test_viewer_session_is_read_only_with_read_tools_only() -> None:
    session = derive_session(_actor("Viewer"), session_id="s", now=FakeClock().now())
    assert session.allowed_tools == ("ticket_get",)
    assert session.write_mode == WRITE_MODE_READ_ONLY


def test_agent_session_follows_token_scopes_not_a_role_matrix() -> None:
    scoped = derive_session(
        _actor("Agent", scopes=("tickets.read", "proposals.write")),
        session_id="s",
        now=FakeClock().now(),
    )
    assert scoped.allowed_tools == ("ticket_get", "agent_action_propose")
    assert scoped.write_mode == WRITE_MODE_PROPOSE_ONLY

    read_only = derive_session(
        _actor("Agent", scopes=("tickets.read",)), session_id="s", now=FakeClock().now()
    )
    assert read_only.allowed_tools == ("ticket_get",)
    assert read_only.write_mode == WRITE_MODE_READ_ONLY

    unscoped = derive_session(_actor("Agent"), session_id="s", now=FakeClock().now())
    assert unscoped.allowed_tools == ()


def test_unknown_role_fails_closed() -> None:
    session = derive_session(_actor("Hacker"), session_id="s", now=FakeClock().now())
    assert session.allowed_tools == ()
    assert session.write_mode == WRITE_MODE_READ_ONLY


def test_session_expiry_is_one_hour_by_default() -> None:
    clock = FakeClock()
    session = derive_session(_actor("Owner"), session_id="s", now=clock.now())
    assert not session.expired(clock.now())
    clock.advance(3599)
    assert not session.expired(clock.now())
    clock.advance(1)
    assert session.expired(clock.now())


def test_rate_window_resets_but_session_total_does_not() -> None:
    clock = FakeClock()
    session = derive_session(_actor("Owner"), session_id="s", now=clock.now())
    for _ in range(RATE_LIMIT_PER_MINUTE):
        assert session.count_call(clock.now())
    # 51st call in the same minute kills the session.
    assert not session.count_call(clock.now())
    assert session.killed

    # A fresh session that paces itself stays alive across windows...
    paced = derive_session(_actor("Owner"), session_id="s2", now=clock.now())
    for _ in range(RATE_LIMIT_PER_MINUTE):
        assert paced.count_call(clock.now())
    clock.advance(60)
    assert paced.count_call(clock.now())
    assert not paced.killed

    # ...until the per-session lifetime cap.
    paced.calls_total = RATE_LIMIT_PER_SESSION
    clock.advance(60)
    assert not paced.count_call(clock.now())
    assert paced.killed


def test_registry_reuses_by_transport_session_and_prunes_dead_ones() -> None:
    clock = FakeClock()
    registry = SessionRegistry(ttl=timedelta(hours=1))
    actor = _actor("Owner")
    first = registry.get_or_create(actor, session_id="s1", now=clock.now())
    again = registry.get_or_create(actor, session_id="s1", now=clock.now())
    assert again is first

    # Two TTLs later the session is long expired; creating a new one prunes it.
    clock.advance(2 * 3600 + 1)
    fresh = registry.get_or_create(actor, session_id="s2", now=clock.now())
    assert fresh is not first
    assert registry.get("s1") is None
