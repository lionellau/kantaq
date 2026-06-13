"""Memory API: entries and ticket links (E13, MOD-19).

The HTTP face of ``kantaq_core.memory`` — every handler delegates to
``MemoryService`` (the one write path: validate → apply → audit → emit-if-team)
and never touches tables directly. Reads need ``memory.read``, writes need
``memory.write`` (Viewer reads, Member and up write, agents by scope).

The privacy boundary lives in the service (NFR-E13-1: a ``visibility=local``
row never produces an event), so this layer stays a thin translation:
``visibility`` is accepted at create only — it is absent from the PATCH shape
and the service rejects it anyway (defense in depth).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.memory import (
    MemoryNotFoundError,
    MemoryService,
    MemoryValidationError,
    domain_visibility,
)
from kantaq_db.models import MemoryEntry, MemoryLink
from kantaq_runtime.auth import get_engine_dep, get_event_signer, require_action
from kantaq_sync_engine import EventLogSink, EventSigner

router = APIRouter(prefix="/v1", tags=["memory"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.memory_read))]
WriterActor = Annotated[VerifiedActor, Depends(require_action(Action.memory_write))]
# Write routes only (it ensures the member's self-grant), so a read never signs.
SignerDep = Annotated[EventSigner | None, Depends(get_event_signer)]


def _service(
    session: Session, actor: VerifiedActor, signer: EventSigner | None = None
) -> MemoryService:
    # Same seam as the tracker API: entity row, audit row, and (team-only)
    # event-log row share one transaction, attributed to the member (E04). The
    # signer (E04-T4) signs each emitted event post-cutover; a ``visibility=local``
    # write produces no event, so it is simply never signed.
    sink = EventLogSink(session, actor.member_id, signer=signer)
    return MemoryService(session, actor_id=actor.member_id, source="app", sink=sink)


# ----------------------------------------------------------------- API shapes


class MemoryOut(BaseModel):
    id: str
    title: str
    body: str
    type: str
    source: str
    space: str
    linked_entities: list[str]
    provenance: dict[str, str]
    confidence: str
    review_status: str
    visibility: str
    domain_visibility: str
    expires_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: MemoryEntry) -> MemoryOut:
        return cls.model_validate(
            {
                **row.model_dump(),
                "domain_visibility": domain_visibility(
                    row.visibility, row.review_status, row.space
                ),
            }
        )


class MemoryIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = ""
    type: str = "note"
    source: str = "manual"
    space: str = "workspace"
    visibility: str = "team"
    confidence: str = "medium"
    linked_entities: list[str] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime | None = None


class MemoryPatch(BaseModel):
    # Fail closed on unknown fields — in particular `visibility`, which is
    # immutable in v0.1 (the service enforces it too; this 422s earlier).
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    body: str | None = None
    type: str | None = None
    source: str | None = None
    space: str | None = None
    linked_entities: list[str] | None = None
    provenance: dict[str, str] | None = None
    confidence: str | None = None
    review_status: str | None = None
    expires_at: datetime | None = None


class MemoryLinkOut(BaseModel):
    id: str
    ticket_id: str
    memory_id: str
    reason: str
    visibility: str
    created_by: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: MemoryLink) -> MemoryLinkOut:
        return cls.model_validate(row, from_attributes=True)


class MemoryLinkIn(BaseModel):
    ticket_id: str
    reason: str = Field(min_length=1, max_length=500)


class LinkedMemoryOut(BaseModel):
    """One linked entry on a ticket: the link (reason) plus the entry
    (with provenance) — what the ticket page renders (E13-T3)."""

    link: MemoryLinkOut
    entry: MemoryOut


# ------------------------------------------------------------- error mapping


def _domain(exc: MemoryNotFoundError | MemoryValidationError) -> HTTPException:
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ------------------------------------------------------------------- entries


@router.get("/memory", response_model=list[MemoryOut])
def list_memory(
    actor: ReaderActor,
    engine: EngineDep,
    space: str | None = None,
    type: str | None = None,  # noqa: A002 — mirrors the field name
    review_status: str | None = None,
    q: str | None = None,
    include_expired: bool = False,
) -> list[MemoryOut]:
    with Session(engine) as session:
        rows = _service(session, actor).list_entries(
            space=space,
            type=type,
            review_status=review_status,
            q=q,
            include_expired=include_expired,
        )
        return [MemoryOut.from_row(row) for row in rows]


@router.post("/memory", response_model=MemoryOut, status_code=201)
def create_memory(
    body: MemoryIn, actor: WriterActor, engine: EngineDep, signer: SignerDep
) -> MemoryOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).create_entry(
                title=body.title,
                body=body.body,
                type=body.type,
                source=body.source,
                space=body.space,
                visibility=body.visibility,
                confidence=body.confidence,
                linked_entities=body.linked_entities,
                provenance=dict(body.provenance),
                expires_at=body.expires_at,
            )
        except (MemoryNotFoundError, MemoryValidationError) as exc:
            raise _domain(exc) from exc
        return MemoryOut.from_row(row)


@router.get("/memory/{memory_id}", response_model=MemoryOut)
def get_memory(memory_id: str, actor: ReaderActor, engine: EngineDep) -> MemoryOut:
    with Session(engine) as session:
        try:
            return MemoryOut.from_row(_service(session, actor).get_entry(memory_id))
        except MemoryNotFoundError as exc:
            raise _domain(exc) from exc


@router.patch("/memory/{memory_id}", response_model=MemoryOut)
def update_memory(
    memory_id: str,
    body: MemoryPatch,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> MemoryOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).update_entry(
                memory_id, body.model_dump(exclude_unset=True)
            )
        except (MemoryNotFoundError, MemoryValidationError) as exc:
            raise _domain(exc) from exc
        return MemoryOut.from_row(row)


@router.delete("/memory/{memory_id}", status_code=204)
def delete_memory(memory_id: str, actor: WriterActor, engine: EngineDep, signer: SignerDep) -> None:
    with Session(engine) as session:
        try:
            _service(session, actor, signer).delete_entry(memory_id)
        except MemoryNotFoundError as exc:
            raise _domain(exc) from exc


# --------------------------------------------------------------------- links


@router.post("/memory/{memory_id}/link", response_model=MemoryLinkOut, status_code=201)
def link_memory(
    memory_id: str,
    body: MemoryLinkIn,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> MemoryLinkOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).link(memory_id, body.ticket_id, body.reason)
        except (MemoryNotFoundError, MemoryValidationError) as exc:
            raise _domain(exc) from exc
        return MemoryLinkOut.from_row(row)


@router.get("/memory/{memory_id}/links", response_model=list[MemoryLinkOut])
def memory_links(memory_id: str, actor: ReaderActor, engine: EngineDep) -> list[MemoryLinkOut]:
    with Session(engine) as session:
        try:
            rows = _service(session, actor).links_for_entry(memory_id)
        except MemoryNotFoundError as exc:
            raise _domain(exc) from exc
        return [MemoryLinkOut.from_row(row) for row in rows]


@router.get("/tickets/{ticket_id}/memory", response_model=list[LinkedMemoryOut])
def ticket_memory(
    ticket_id: str,
    actor: ReaderActor,
    engine: EngineDep,
    include_expired: bool = False,
) -> list[LinkedMemoryOut]:
    """The ticket's linked memory with provenance, for the ticket page."""
    with Session(engine) as session:
        try:
            pairs = _service(session, actor).linked_memory(
                ticket_id, include_expired=include_expired
            )
        except MemoryNotFoundError as exc:
            raise _domain(exc) from exc
        return [
            LinkedMemoryOut(link=MemoryLinkOut.from_row(link), entry=MemoryOut.from_row(entry))
            for link, entry in pairs
        ]
