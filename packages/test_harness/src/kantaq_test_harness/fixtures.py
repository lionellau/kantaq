"""Pytest fixtures, auto-loaded via the ``pytest11`` entry point (see pyproject).

Any test in any package can request ``fake_clock``, ``seeded_random``, or
``fake_backend`` without a local conftest.

This module is imported at *plugin registration*, before pytest-cov starts
measuring. Anything imported here transitively is invisible to coverage —
``FakeBackend`` now reaches ``kantaq_core`` (via the canonical protocol Event),
so its import is deferred into the fixture body. Keep new imports here to the
stdlib-only fakes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.engine import Engine

from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.db import temp_sqlite_engine
from kantaq_test_harness.keychain import FakeKeychain
from kantaq_test_harness.random import SeededRandom

if TYPE_CHECKING:
    from kantaq_test_harness.backend import FakeBackend


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_keychain() -> FakeKeychain:
    return FakeKeychain()


@pytest.fixture
def seeded_random() -> SeededRandom:
    return SeededRandom(0)


@pytest.fixture
def fake_backend() -> FakeBackend:
    from kantaq_test_harness.backend import FakeBackend

    return FakeBackend()


@pytest.fixture
def temp_sqlite(tmp_path: Path) -> Iterator[Engine]:
    """A throwaway file-backed SQLite engine for Domain/migration tests."""
    with temp_sqlite_engine(tmp_path) as engine:
        yield engine
