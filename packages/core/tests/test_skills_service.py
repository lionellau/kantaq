"""Skill registry CRUD, validation, audit, and the off-sync invariant (E17-T4).

The registry is db-backed but OFF the sync surface (v0.2): these tests pin the
CRUD round-trip, the fail-closed vocabularies, not-found / FK integrity, that
every write lands an attributed audit row (MOD-07), and — the defining property
— that NO sync event is ever emitted (the ``event_log`` stays empty), mirroring
the memory service's local-write privacy test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from kantaq_core.skills import (
    SkillNotFoundError,
    SkillRegistryService,
    SkillValidationError,
)
from kantaq_db.models import AuditEvent, EventLog
from kantaq_test_harness.clock import FakeClock

ACTOR = "mbr_actor000001"


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(session: Session, clock: FakeClock) -> SkillRegistryService:
    return SkillRegistryService(session, actor_id=ACTOR, source="app", now=clock.now)


def _audit_rows(session: Session, action: str) -> list[AuditEvent]:
    rows = session.exec(select(AuditEvent).where(AuditEvent.action == action)).all()
    return sorted(rows, key=lambda r: r.id)


def _event_log_rows(session: Session) -> list[EventLog]:
    return list(session.exec(select(EventLog)).all())


# --------------------------------------------------------------- round-trip


def test_container_and_mapping_crud_round_trip(service: SkillRegistryService) -> None:
    container = service.create_container(
        slug="  repo-investigation  ",
        name="Repo investigation",
        recommended_roles=["code_agent"],
        supported_stages=["implementation"],
        allowed_tools=["role_context_get", "ticket_get"],
    )
    assert container.slug == "repo-investigation"  # stripped
    assert container.recommended_roles == ["code_agent"]
    assert container.default_write_mode == "read"
    assert container.risk_level == "low"

    assert service.get_container(container.id).id == container.id
    assert [c.slug for c in service.list_containers()] == ["repo-investigation"]

    updated = service.update_container(container.id, {"risk_level": "high", "name": "Repo dive"})
    assert updated.risk_level == "high"
    assert updated.name == "Repo dive"

    mapping = service.create_mapping(
        container_id=container.id,
        scope="workspace",
        provider="anthropic",
        connection="an MCP-connected coding agent",
    )
    assert mapping.container_id == container.id
    assert mapping.created_by == ACTOR
    assert mapping.scope == "workspace"

    assert service.get_mapping(mapping.id).id == mapping.id
    assert [m.id for m in service.list_mappings(container_id=container.id)] == [mapping.id]
    assert service.list_mappings(scope="personal") == []

    remapped = service.update_mapping(mapping.id, {"status": "disabled"})
    assert remapped.status == "disabled"

    service.delete_mapping(mapping.id)
    with pytest.raises(SkillNotFoundError):
        service.get_mapping(mapping.id)
    service.delete_container(container.id)
    with pytest.raises(SkillNotFoundError):
        service.get_container(container.id)


# --------------------------------------------------------- validation (fail-closed)


def _container(service: SkillRegistryService, **overrides: object) -> str:
    base: dict[str, object] = {
        "slug": "c1",
        "name": "C1",
        "recommended_roles": ["code_agent"],
        "supported_stages": ["implementation"],
    }
    base.update(overrides)
    return service.create_container(**base).id  # type: ignore[arg-type]


def test_create_rejects_bad_write_mode(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="unknown default_write_mode"):
        _container(service, default_write_mode="bogus")


def test_create_rejects_bad_risk_level(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="unknown risk_level"):
        _container(service, risk_level="severe")


def test_create_rejects_bad_role(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="unknown role"):
        _container(service, recommended_roles=["wizard_agent"])


def test_create_rejects_bad_stage(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="unknown stage"):
        _container(service, supported_stages=["liftoff"])


def test_create_rejects_empty_slug(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="non-empty slug"):
        _container(service, slug="   ")


def test_create_rejects_oversized_slug(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="slug exceeds"):
        _container(service, slug="x" * 33)


def test_create_rejects_oversized_name(service: SkillRegistryService) -> None:
    with pytest.raises(SkillValidationError, match="name exceeds"):
        _container(service, name="x" * 65)


def test_create_rejects_duplicate_slug(service: SkillRegistryService) -> None:
    _container(service, slug="dup")
    with pytest.raises(SkillValidationError, match="exists"):
        _container(service, slug="dup")


def test_mapping_rejects_bad_scope(service: SkillRegistryService) -> None:
    cid = _container(service)
    with pytest.raises(SkillValidationError, match="unknown scope"):
        service.create_mapping(container_id=cid, scope="global")


def test_mapping_rejects_bad_status(service: SkillRegistryService) -> None:
    cid = _container(service)
    with pytest.raises(SkillValidationError, match="unknown status"):
        service.create_mapping(container_id=cid, status="paused")


def test_delete_container_with_mappings_is_refused(service: SkillRegistryService) -> None:
    """The FK has no cascade by design — deleting a container out from under its
    mappings would strand them, so it is refused with a clean domain error
    (not a raw IntegrityError); deleting the mapping first then succeeds."""
    cid = _container(service)
    mapping = service.create_mapping(container_id=cid, provider="p")
    with pytest.raises(SkillValidationError, match="still has mappings"):
        service.delete_container(cid)
    service.delete_mapping(mapping.id)
    service.delete_container(cid)  # now clear
    with pytest.raises(SkillNotFoundError):
        service.get_container(cid)


def test_update_container_rejects_slug_mutation(service: SkillRegistryService) -> None:
    cid = _container(service)
    with pytest.raises(SkillValidationError, match="slug is immutable"):
        service.update_container(cid, {"slug": "renamed"})


def test_update_container_rejects_unknown_field(service: SkillRegistryService) -> None:
    cid = _container(service)
    with pytest.raises(SkillValidationError, match="unknown skill container fields"):
        service.update_container(cid, {"bogus": "x"})


def test_update_mapping_rejects_container_id_mutation(service: SkillRegistryService) -> None:
    cid = _container(service)
    mid = service.create_mapping(container_id=cid).id
    with pytest.raises(SkillValidationError, match="container_id is immutable"):
        service.update_mapping(mid, {"container_id": "other"})


# ---------------------------------------------------------- not found / FK


def test_get_update_delete_missing_container(service: SkillRegistryService) -> None:
    with pytest.raises(SkillNotFoundError):
        service.get_container("skc_missing")
    with pytest.raises(SkillNotFoundError):
        service.update_container("skc_missing", {"name": "x"})
    with pytest.raises(SkillNotFoundError):
        service.delete_container("skc_missing")


def test_get_update_delete_missing_mapping(service: SkillRegistryService) -> None:
    with pytest.raises(SkillNotFoundError):
        service.get_mapping("skm_missing")
    with pytest.raises(SkillNotFoundError):
        service.update_mapping("skm_missing", {"status": "active"})
    with pytest.raises(SkillNotFoundError):
        service.delete_mapping("skm_missing")


def test_mapping_to_missing_container_rejected(service: SkillRegistryService) -> None:
    """FK integrity surfaces as a clean 422, not a raw IntegrityError."""
    with pytest.raises(SkillValidationError, match="no such skill container"):
        service.create_mapping(container_id="skc_missing")


# ------------------------------------------------------------------- audit


def test_every_write_records_an_audit_row(service: SkillRegistryService, session: Session) -> None:
    container = service.create_container(
        slug="c", name="C", recommended_roles=["code_agent"], supported_stages=["implementation"]
    )
    service.update_container(container.id, {"risk_level": "high"})
    mapping = service.create_mapping(container_id=container.id)
    service.update_mapping(mapping.id, {"status": "disabled"})
    service.delete_mapping(mapping.id)
    service.delete_container(container.id)

    for action in (
        "skill_container.create",
        "skill_container.update",
        "skill_container.delete",
        "skill_mapping.create",
        "skill_mapping.update",
        "skill_mapping.delete",
    ):
        rows = _audit_rows(session, action)
        assert len(rows) == 1, action
        assert rows[0].actor_id == ACTOR
        assert rows[0].source == "app"
        assert rows[0].object_ref is not None


def test_audit_object_ref_points_at_the_row(
    service: SkillRegistryService, session: Session
) -> None:
    container = service.create_container(
        slug="c", name="C", recommended_roles=["code_agent"], supported_stages=["implementation"]
    )
    row = _audit_rows(session, "skill_container.create")[0]
    assert row.object_ref == f"skill_containers/{container.id}"
    assert row.after is not None and row.after["slug"] == "c"


# ----------------------------------------------------- the off-sync invariant


def test_registry_writes_never_enter_the_event_log(
    service: SkillRegistryService, session: Session
) -> None:
    """The defining property: the registry is off the sync surface, so no write
    produces an ``event_log`` row (contrast memory's emit seam)."""
    container = service.create_container(
        slug="c", name="C", recommended_roles=["code_agent"], supported_stages=["implementation"]
    )
    service.update_container(container.id, {"risk_level": "high"})
    mapping = service.create_mapping(container_id=container.id)
    service.update_mapping(mapping.id, {"status": "disabled"})
    service.delete_mapping(mapping.id)
    service.delete_container(container.id)

    assert _event_log_rows(session) == []
