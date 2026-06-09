"""FakeClock — deterministic, injectable time (no global monkeypatching)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """A clock tests control explicitly. Pass it where real code would read time."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or _EPOCH

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def monotonic(self) -> float:
        """Seconds since the epoch — a drop-in for ``time.monotonic`` in tests."""
        return self._now.timestamp()
