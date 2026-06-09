"""HeroFlowTimer — time the hero flow and assert it stays under budget.

PRD §1.1 ("if any step takes meaningfully longer, that step is broken") and §20.1
(the team hero flow completes in under 15 minutes). The clock is injectable so the
gate itself is testable: pass a fake clock to simulate an over-budget run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType

DEFAULT_BUDGET_SECONDS = 15 * 60


class HeroFlowTooSlow(AssertionError):
    pass


class HeroFlowTimer:
    def __init__(
        self,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = budget_seconds
        self._clock = clock
        self._start = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> HeroFlowTimer:
        self._start = self._clock()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.elapsed = self._clock() - self._start

    def assert_under_budget(self) -> None:
        if self.elapsed > self._budget:
            raise HeroFlowTooSlow(
                f"hero flow took {self.elapsed:.1f}s, over the {self._budget:.0f}s budget"
            )
