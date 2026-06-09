"""FakeClock: deterministic and controllable."""

from datetime import timedelta

from kantaq_test_harness import FakeClock


def test_clock_starts_fixed_and_advances() -> None:
    clock = FakeClock()
    t0 = clock.now()
    t1 = clock.advance(60)
    assert t1 - t0 == timedelta(seconds=60)
    assert clock.now() == t1


def test_two_clocks_same_start_are_equal() -> None:
    assert FakeClock().now() == FakeClock().now()


def test_monotonic_tracks_advance() -> None:
    clock = FakeClock()
    before = clock.monotonic()
    clock.advance(5)
    assert clock.monotonic() - before == 5
