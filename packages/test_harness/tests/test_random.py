"""SeededRandom: reproducible across runs (determinism guard)."""

from kantaq_test_harness import SeededRandom


def test_same_seed_reproduces_sequence() -> None:
    a = SeededRandom(42)
    b = SeededRandom(42)
    assert [a.token() for _ in range(5)] == [b.token() for _ in range(5)]


def test_different_seed_differs() -> None:
    assert SeededRandom(1).token() != SeededRandom(2).token()


def test_sortable_id_is_sortable_and_unique() -> None:
    rng = SeededRandom(0)
    ids = [rng.sortable_id() for _ in range(100)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 100


def test_choice_is_deterministic() -> None:
    items = ["a", "b", "c", "d"]
    assert SeededRandom(7).choice(items) == SeededRandom(7).choice(items)
