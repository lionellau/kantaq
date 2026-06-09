"""The pytest11 plugin auto-loads fixtures into every package's tests."""

from kantaq_test_harness import FakeBackend, FakeClock, SeededRandom


def test_harness_fixtures_are_available(
    fake_clock: FakeClock,
    seeded_random: SeededRandom,
    fake_backend: FakeBackend,
) -> None:
    assert isinstance(fake_clock, FakeClock)
    assert isinstance(seeded_random, SeededRandom)
    assert isinstance(fake_backend, FakeBackend)
