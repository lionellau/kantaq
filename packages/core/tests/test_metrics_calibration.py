"""MOD-27 FR-E26-1: the rows/bytes estimate lands within 10% of the DB catalog.

The accuracy gate. Seeds the as-built 6-month 4-person profile into
EphemeralPostgres, reads ``pg_total_relation_size`` (the ground truth), and
asserts ``kantaq_core.metrics.model_backend_bytes`` lands within 10% of it — per
the spec's granularity: the five dominant tables individually, the "other
syncable" group, and the total (MOD-27 build notes). Postgres-gated: skips when
``KANTAQ_TEST_POSTGRES_URL`` is unset (local dev, fresh-clone); runs in CI.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from kantaq_core.metrics import model_backend_bytes
from kantaq_test_harness.cost_profile import seed_cost_profile
from kantaq_test_harness.db import EphemeralPostgres

TOLERANCE = 0.10
# The spec breaks these out individually; the rest fold into the "other" group.
DOMINANT = ("audit_events", "sync_events", "tickets", "comments", "memory_entries")
OTHER = (
    "projects",
    "members",
    "workspaces",
    "ticket_relationships",
    "memory_links",
    "agent_proposals",
)


@pytest.mark.skipif(not EphemeralPostgres.available(), reason="no KANTAQ_TEST_POSTGRES_URL")
def test_estimate_within_10pct_of_catalog() -> None:
    with EphemeralPostgres() as engine:
        counts = seed_cost_profile(engine, scale=1.0)
        with engine.connect() as conn:
            measured = {
                str(name): int(size)
                for name, size in conn.execute(
                    text("SELECT relname, pg_total_relation_size(relid) FROM pg_stat_user_tables")
                ).all()
            }

    est = model_backend_bytes(counts)

    # Per-table for the dominant tables.
    for table in DOMINANT:
        err = abs(est[table] - measured[table]) / measured[table]
        assert err <= TOLERANCE, (
            f"{table}: est={est[table]} measured={measured[table]} err={err:.1%}"
        )

    # The "other syncable" group (the tiny-cardinality tables fold in here, as in
    # the spec's calibration table — their fixed-page overhead is absorbed).
    est_other = sum(est[t] for t in OTHER)
    meas_other = sum(measured[t] for t in OTHER)
    err_other = abs(est_other - meas_other) / meas_other
    assert err_other <= TOLERANCE, (
        f"other group: est={est_other} measured={meas_other} err={err_other:.1%}"
    )

    # The total over the cost-relevant profile tables.
    profile = (*DOMINANT, *OTHER)
    est_total = sum(est[t] for t in profile)
    meas_total = sum(measured[t] for t in profile)
    err_total = abs(est_total - meas_total) / meas_total
    assert err_total <= TOLERANCE, (
        f"total: est={est_total} measured={meas_total} err={err_total:.1%}"
    )


@pytest.mark.skipif(not EphemeralPostgres.available(), reason="no KANTAQ_TEST_POSTGRES_URL")
def test_rows_are_a_census_not_an_estimate() -> None:
    """Row counts are exact (a census), not modeled — the spec's 0.0% row error."""
    with EphemeralPostgres() as engine:
        counts = seed_cost_profile(engine, scale=1.0)
        with engine.connect() as conn:
            for table, seeded in counts.items():
                actual = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                assert int(actual or 0) == seeded, f"{table}: {actual} != {seeded}"
