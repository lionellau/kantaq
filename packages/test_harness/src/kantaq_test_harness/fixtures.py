"""Pytest fixtures, auto-loaded via the ``pytest11`` entry point (see pyproject).

Any test in any package can request ``fake_clock``, ``seeded_random``, or
``fake_backend`` without a local conftest.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.db import temp_sqlite_engine
from kantaq_test_harness.random import SeededRandom


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def seeded_random() -> SeededRandom:
    return SeededRandom(0)


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def temp_sqlite(tmp_path: Path) -> Iterator[Engine]:
    """A throwaway file-backed SQLite engine for Domain/migration tests."""
    with temp_sqlite_engine(tmp_path) as engine:
        yield engine
