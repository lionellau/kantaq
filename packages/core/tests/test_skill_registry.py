"""The db registry seed matches the hardcoded reco contract (E17-T4).

The 0010 migration moves the 29 hardcoded ``kantaq_core.reco.CONTAINERS`` behind
a db table "behind the same contract". This test runs the real migration onto a
throwaway SQLite db and pins the seeded ``skill_containers`` rows against the
still-pure engine registry — exact slug set, plus the per-container fields — so
the two can never drift. The migration itself never imports kantaq_core (its
seed is static literals); this test is where the contract is re-asserted.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from kantaq_core import reco
from kantaq_db import migrations
from kantaq_db.models import SkillContainerRow
from kantaq_db.session import get_engine


def _migrated_containers(tmp_path: Path) -> list[SkillContainerRow]:
    url = f"sqlite:///{tmp_path / 'registry.sqlite'}"
    migrations.upgrade(url)
    with Session(get_engine(url)) as session:
        return list(session.exec(select(SkillContainerRow)).all())


def test_db_registry_matches_the_hardcoded_contract(tmp_path: Path) -> None:
    rows = _migrated_containers(tmp_path)
    by_slug = {row.slug: row for row in rows}

    # Exact slug set: no missing, no orphan (the migration moved every container).
    assert set(by_slug) == {c.id for c in reco.CONTAINERS}

    for container in reco.CONTAINERS:
        row = by_slug[container.id]
        assert row.name == container.name
        # recommended_role (singular, v0.1) seeds as a one-element plural list.
        assert row.recommended_roles == [container.recommended_role]
        assert row.supported_stages == list(container.supported_stages)
        assert row.default_write_mode == container.default_write_mode
        assert row.risk_level == container.risk_level
        assert row.allowed_tools == list(container.allowed_tools)
