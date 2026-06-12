"""Sync-engine test fixtures over the MOD-30 two-replica simulator."""

from __future__ import annotations

from pathlib import Path

import pytest

from kantaq_test_harness.backend import FakeBackend
from kantaq_test_harness.replica import Replica, make_replica


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def alice(tmp_path: Path, backend: FakeBackend) -> Replica:
    return make_replica(tmp_path, "alice", backend)


@pytest.fixture
def bob(tmp_path: Path, backend: FakeBackend) -> Replica:
    return make_replica(tmp_path, "bob", backend)
