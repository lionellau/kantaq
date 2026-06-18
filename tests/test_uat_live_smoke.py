"""Hermetic self-test for the DEBT-30 live timed-smoke harness (scripts/uat_live_smoke.py).

The harness itself is opt-in (network + a signed-in session) and not a CI gate.
This test proves its *logic* deterministically: the in-process checks measure and
classify correctly, the gates can FAIL (MOD-30 — a gate that cannot fail proves
nothing), and the live checks SKIP without a session (or FAIL under --require-live).

It deliberately does NOT assert the tight 50 ms budget — that would add another
flaky perf gate (cf. NFR-E12-1). The pass-path uses a generous budget; the real
budget is enforced by the harness on its live run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

import kantaq_db.models  # noqa: F401 — register all tables on SQLModel.metadata
from kantaq_test_harness.keychain import FakeKeychain

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "uat_live_smoke", ROOT / "scripts" / "uat_live_smoke.py"
)
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke  # so @dataclass in the script can resolve its module
_spec.loader.exec_module(smoke)


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """A fresh seeded world on a file-backed SQLite (metadata-created, no migrate)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'smoke.sqlite'}")
    SQLModel.metadata.create_all(engine)
    return smoke.seed_world(engine, FakeKeychain())


# ----------------------------------------------------------- gateway latency


def test_gateway_latency_measures_and_passes_generous_budget(world: dict) -> None:
    res = smoke.check_gateway_latency(world, samples=60, budget_ms=10_000)
    assert res.status == "PASS", res.detail
    assert "P50" in res.measured and "P95" in res.measured


def test_gateway_latency_gate_can_fail(world: dict) -> None:
    # MOD-30: an impossible budget must trip the gate, or it proves nothing.
    res = smoke.check_gateway_latency(world, samples=60, budget_ms=0.0)
    assert res.status == "FAIL"


def test_gateway_latency_stays_under_the_per_minute_rate_cut(world: dict) -> None:
    # >50 samples would terminate a single session; the harness rolls sessions.
    res = smoke.check_gateway_latency(world, samples=130, budget_ms=10_000)
    assert res.status == "PASS", res.detail


# ------------------------------------------------------------ revoke recheck


def test_revoke_recheck_denies_within_budget(world: dict) -> None:
    res = smoke.check_revoke_recheck(world, budget_s=5.0)
    assert res.status == "PASS", res.detail
    assert "identity" in res.detail


def test_revoke_recheck_gate_can_fail(world: dict) -> None:
    res = smoke.check_revoke_recheck(world, budget_s=0.0)
    assert res.status == "FAIL"


# -------------------------------------------------------------- live gating


def test_live_checks_skip_without_a_session() -> None:
    assert smoke.check_revoke_xreplica(None).status == "SKIP"
    assert smoke.check_retention_mark(None).status == "SKIP"


def test_require_live_turns_skip_into_fail() -> None:
    assert smoke.check_revoke_xreplica(None, require_live=True).status == "FAIL"
    assert smoke.check_retention_mark(None, require_live=True).status == "FAIL"
