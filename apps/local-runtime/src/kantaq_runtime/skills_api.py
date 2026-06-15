"""Skills API: the db-backed skill registry — containers + mappings (E17-T5).

The HTTP face of ``kantaq_core.skills.SkillRegistryService``. It backs the
Settings skill-mapping editor: list the containers (the taxonomy, for the
picker), then create/update/delete the personal/workspace skill→tool mappings
that the recommendation panel reflects (FR-E17-2). Reads need ``skills.read``
(every human); managing a mapping needs ``skills.manage`` (Member and up).

The registry is **off the sync surface** (architecture §6.1), so unlike the
memory/tracker APIs there is no ``EventLogSink`` and no signer here — the service
writes locally + audits and never emits. ``connection`` is a descriptive label,
never an executable binding and never a secret (DEBT-06 / DEBT-07).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.skills import (
    SkillNotFoundError,
    SkillRegistryService,
    SkillValidationError,
)
from kantaq_db.models import SkillContainerRow, SkillMappingRow
from kantaq_runtime.auth import get_engine_dep, require_action

router = APIRouter(prefix="/v1", tags=["skills"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.skills_read))]
ManagerActor = Annotated[VerifiedActor, Depends(require_action(Action.skills_manage))]


def _service(session: Session, actor: VerifiedActor) -> SkillRegistryService:
    # Sink-less by design: the registry is off the sync surface, so the service
    # writes locally + audits and never emits an event (contrast memory_api).
    return SkillRegistryService(session, actor_id=actor.member_id)


def _domain(exc: SkillNotFoundError | SkillValidationError) -> HTTPException:
    if isinstance(exc, SkillNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ----------------------------------------------------------------- API shapes


class SkillContainerOut(BaseModel):
    """A skill container (the taxonomy unit) — read-only over this surface."""

    id: str
    slug: str
    name: str
    recommended_roles: list[str]
    supported_stages: list[str]
    required_input: str
    expected_output: str
    allowed_tools: list[str]
    default_write_mode: str
    risk_level: str

    @classmethod
    def from_row(cls, row: SkillContainerRow) -> SkillContainerOut:
        return cls(
            id=row.id,
            slug=row.slug,
            name=row.name,
            recommended_roles=list(row.recommended_roles),
            supported_stages=list(row.supported_stages),
            required_input=row.required_input,
            expected_output=row.expected_output,
            allowed_tools=list(row.allowed_tools),
            default_write_mode=row.default_write_mode,
            risk_level=row.risk_level,
        )


class SkillMappingOut(BaseModel):
    """A personal/workspace skill→tool mapping."""

    id: str
    container_id: str
    scope: str
    provider: str
    connection: str
    status: str
    created_by: str | None

    @classmethod
    def from_row(cls, row: SkillMappingRow) -> SkillMappingOut:
        return cls(
            id=row.id,
            container_id=row.container_id,
            scope=row.scope,
            provider=row.provider,
            connection=row.connection,
            status=row.status,
            created_by=row.created_by,
        )


class SkillMappingIn(BaseModel):
    """Create a mapping. ``connection`` is a descriptive label, never a secret."""

    model_config = ConfigDict(extra="forbid")

    container_id: str
    scope: str = "personal"
    provider: str = ""
    connection: str = ""
    status: str = "active"


class SkillMappingPatch(BaseModel):
    """Patch a mapping. ``container_id`` is immutable (re-point = delete + create)."""

    model_config = ConfigDict(extra="forbid")

    scope: str | None = Field(default=None)
    provider: str | None = Field(default=None)
    connection: str | None = Field(default=None)
    status: str | None = Field(default=None)


# ----------------------------------------------------------------- containers


@router.get("/skill-containers", response_model=list[SkillContainerOut])
def list_skill_containers(actor: ReaderActor, engine: EngineDep) -> list[SkillContainerOut]:
    """Every skill container, by slug — the picker for the mapping editor."""
    with Session(engine) as session:
        rows = _service(session, actor).list_containers()
        return [SkillContainerOut.from_row(row) for row in rows]


# ------------------------------------------------------------------ mappings


@router.get("/skill-mappings", response_model=list[SkillMappingOut])
def list_skill_mappings(
    actor: ReaderActor,
    engine: EngineDep,
    container_id: str | None = None,
    scope: str | None = None,
) -> list[SkillMappingOut]:
    with Session(engine) as session:
        rows = _service(session, actor).list_mappings(container_id=container_id, scope=scope)
        return [SkillMappingOut.from_row(row) for row in rows]


@router.post("/skill-mappings", response_model=SkillMappingOut, status_code=201)
def create_skill_mapping(
    body: SkillMappingIn, actor: ManagerActor, engine: EngineDep
) -> SkillMappingOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor).create_mapping(
                container_id=body.container_id,
                scope=body.scope,
                provider=body.provider,
                connection=body.connection,
                status=body.status,
            )
        except (SkillNotFoundError, SkillValidationError) as exc:
            raise _domain(exc) from exc
        return SkillMappingOut.from_row(row)


@router.patch("/skill-mappings/{mapping_id}", response_model=SkillMappingOut)
def update_skill_mapping(
    mapping_id: str, body: SkillMappingPatch, actor: ManagerActor, engine: EngineDep
) -> SkillMappingOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor).update_mapping(
                mapping_id, body.model_dump(exclude_unset=True)
            )
        except (SkillNotFoundError, SkillValidationError) as exc:
            raise _domain(exc) from exc
        return SkillMappingOut.from_row(row)


@router.delete("/skill-mappings/{mapping_id}", status_code=204)
def delete_skill_mapping(mapping_id: str, actor: ManagerActor, engine: EngineDep) -> None:
    with Session(engine) as session:
        try:
            _service(session, actor).delete_mapping(mapping_id)
        except (SkillNotFoundError, SkillValidationError) as exc:
            raise _domain(exc) from exc
