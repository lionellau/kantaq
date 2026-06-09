"""SeededRandom — reproducible ids and choices for tests. Never for crypto."""

from __future__ import annotations

import random
import string
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

_ALPHABET = string.ascii_lowercase + string.digits


class SeededRandom:
    """Deterministic RNG: the same seed yields the same sequence every run."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)
        self._counter = 0

    def integer(self, low: int, high: int) -> int:
        return self._rng.randint(low, high)

    def choice(self, items: Sequence[T]) -> T:
        return self._rng.choice(list(items))

    def token(self, length: int = 12) -> str:
        return "".join(self._rng.choice(_ALPHABET) for _ in range(length))

    def ident(self, prefix: str) -> str:
        return f"{prefix}_{self.token(10)}"

    def ulid(self) -> str:
        """A sortable, deterministic id: monotonic counter + seeded suffix."""
        self._counter += 1
        return f"{self._counter:012d}{self.token(8)}"
