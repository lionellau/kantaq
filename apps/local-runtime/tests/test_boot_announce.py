"""DEBT-45 §2a at the boot seam: the runtime announces the member it IS.

``ensure_device_identity`` emits the member announce **before** the device
event, so every post-fix stream folds member-then-device on a peer and the
``devices.member_id`` FK holds without the §2b placeholder. Idempotent across
boots, exactly like the device registration beside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.identity import IdentityService
from kantaq_db.models import EventLog
from kantaq_runtime.auth import ensure_device_identity, keychain_for
from kantaq_runtime.config import Settings


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


def _settings(tmp_path: Path) -> Settings:
    return Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))


def _log_rows(engine: Engine, collection: str) -> list[EventLog]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(EventLog)
                .where(EventLog.collection == collection)
                .order_by(EventLog.actor_seq)  # type: ignore[arg-type]
            ).all()
        )


def test_boot_announces_the_member_before_the_device_event(engine: Engine, tmp_path: Path) -> None:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None

    ensure_device_identity(engine, keychain_for(_settings(tmp_path)))

    members = _log_rows(engine, "members")
    devices = _log_rows(engine, "devices")
    assert len(members) == 1 and len(devices) == 1
    assert members[0].entity_id == minted.member_id  # announces the member we ARE
    assert members[0].actor_id == minted.member_id  # authored as ourselves (DEBT-42)
    assert members[0].actor_seq < devices[0].actor_seq  # member folds first on a peer
    assert members[0].payload["role"] == "Owner"


def test_boot_announce_is_idempotent_and_backfills_a_legacy_runtime(
    engine: Engine, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None

    ensure_device_identity(engine, keychain_for(settings))
    ensure_device_identity(engine, keychain_for(settings))  # the next boot

    assert len(_log_rows(engine, "members")) == 1  # no duplicate announce

    # A pre-fix runtime: device event already in the log, no announce anywhere
    # (delete it to simulate). The next boot backfills — the §6 self-heal.
    with Session(engine) as session:
        for row in session.exec(select(EventLog).where(EventLog.collection == "members")).all():
            session.delete(row)
        session.commit()
    ensure_device_identity(engine, keychain_for(settings))
    assert len(_log_rows(engine, "members")) == 1
