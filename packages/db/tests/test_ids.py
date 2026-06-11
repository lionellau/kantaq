"""ULID correctness (MOD-02). We claim ULID, so we prove ULID."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import kantaq_db.ids as ids
from kantaq_db.ids import ULID_LEN, is_ulid, new_ulid, ulid_timestamp_ms

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


@pytest.fixture(autouse=True)
def _reset_monotonic_state() -> Iterator[None]:
    # The monotonic factory keeps module-global state; reset it so tests do not
    # depend on each other's order.
    ids._last_ms = -1
    ids._last_random = 0
    yield


def test_ulid_is_26_crockford_chars() -> None:
    value = new_ulid()
    assert len(value) == ULID_LEN == 26
    assert set(value) <= _CROCKFORD
    assert is_ulid(value)


def test_ids_sort_in_creation_order() -> None:
    values = [new_ulid() for _ in range(1000)]
    assert values == sorted(values)
    assert len(set(values)) == 1000  # no collisions


def test_timestamp_is_recoverable() -> None:
    ts = 1_700_000_000_000
    value = new_ulid(ts)
    assert ulid_timestamp_ms(value) == ts


def test_same_millisecond_stays_monotonic() -> None:
    ts = 1_700_000_000_000
    values = [new_ulid(ts) for _ in range(50)]
    assert values == sorted(values)
    assert len(set(values)) == 50
    # All share the same encoded timestamp prefix.
    assert all(ulid_timestamp_ms(v) == ts for v in values)


def test_clock_going_backwards_does_not_break_ordering() -> None:
    later = new_ulid(1_700_000_001_000)
    earlier = new_ulid(1_700_000_000_000)  # earlier wall clock
    assert earlier > later  # monotonicity is preserved despite the backwards clock


@pytest.mark.parametrize(
    "bad",
    ["", "tooshort", "I" * 26, "l" * 26, "0" * 25, "0" * 27, "01KTVEEE3F4VKB8GMWTDNA68F!"],
)
def test_is_ulid_rejects_invalid(bad: str) -> None:
    assert not is_ulid(bad)


def test_ulid_timestamp_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        ulid_timestamp_ms("not-a-ulid")
