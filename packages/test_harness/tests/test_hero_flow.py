"""HeroFlowTimer: passes under budget, fails closed over budget (gate self-test)."""

import pytest

from kantaq_test_harness import FakeClock, HeroFlowTimer, HeroFlowTooSlow


def test_timer_passes_under_budget() -> None:
    clock = FakeClock()
    with HeroFlowTimer(budget_seconds=900, clock=clock.monotonic) as timer:
        clock.advance(10)
    timer.assert_under_budget()
    assert timer.elapsed == 10


def test_timer_fails_closed_over_budget() -> None:
    clock = FakeClock()
    with HeroFlowTimer(budget_seconds=900, clock=clock.monotonic) as timer:
        clock.advance(901)
    with pytest.raises(HeroFlowTooSlow):
        timer.assert_under_budget()
