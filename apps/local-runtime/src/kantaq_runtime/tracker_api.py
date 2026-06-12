"""Tracker API: projects, tickets, comments, activity, attachments (E12, MOD-03).

The HTTP face of ``kantaq_core.tracker`` — every handler delegates to
``TrackerService`` (the one write path: validate → apply → audit → emit) and
never touches tables directly. Reads need ``tickets.read``, writes need
``tickets.write`` (Viewer reads, Member and up write, agents by scope).

Attachments are untrusted files (PRD §15): uploads are stored opaque bytes in
the local blob store; downloads always come back as ``application/octet-stream``
with ``Content-Disposition: attachment`` and ``nosniff`` so a browser saves the
file instead of rendering or executing whatever it claims to be.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.tracker import (
    MAX_ATTACHMENT_BYTES,
    BlobNotFoundError,
    LocalBlobStore,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
)
from kantaq_db.models import AuditEvent, Comment, Project, Ticket, Workspace
from kantaq_runtime.auth import get_engine_dep, require_action
from kantaq_runtime.config import Settings
from kantaq_sync_engine import EventLogSink

router = APIRouter(prefix="/v1", tags=["tracker"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]
WriterActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_write))]


def blob_store_for(settings: Settings) -> LocalBlobStore:
    """The attachment blob store: ``<db dir>/blobs`` in solo mode (D-13)."""
    return LocalBlobStore(Path(settings.local_db_path).parent / "blobs")


def _service(session: Session, actor: VerifiedActor) -> TrackerService:
    # The sink closes the MOD-03 rule "all writes go through the sync engine
    # as Events": entity row, audit row, and event-log row share one
    # transaction, attributed to the authenticated member (E04).
    sink = EventLogSink(session, actor.member_id)
    return TrackerService(session, actor_id=actor.member_id, source="app", sink=sink)


def _store(request: Request) -> LocalBlobStore:
    settings: Settings = request.app.state.settings
    return blob_store_for(settings)


# ----------------------------------------------------------------- API shapes


class ProjectOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    goal: str
    scope: str
    owner: str | None
    target_date: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Project) -> ProjectOut:
        return cls.model_validate(row, from_attributes=True)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    workspace_id: str | None = None  # default: the only workspace
    goal: str = ""
    scope: str = ""
    owner: str | None = None
    target_date: datetime | None = None
    status: str = "active"


class ProjectPatch(BaseModel):
    name: str | None = None
    goal: str | None = None
    scope: str | None = None
    owner: str | None = None
    target_date: datetime | None = None
    status: str | None = None


class AttachmentOut(BaseModel):
    blob_id: str
    filename: str
    media_type: str
    size_bytes: int


class TicketOut(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    status: str
    priority: str
    labels: list[str]
    assignee: str | None
    due_date: datetime | None
    acceptance_criteria: str
    lifecycle_stage: str
    parent_id: str | None
    created_by: str | None
    attachments: list[AttachmentOut]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Ticket) -> TicketOut:
        return cls.model_validate(row, from_attributes=True)


class TicketIn(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    status: str = "todo"
    priority: str = "medium"
    labels: list[str] = Field(default_factory=list)
    assignee: str | None = None
    due_date: datetime | None = None
    acceptance_criteria: str = ""
    lifecycle_stage: str = "intake"
    parent_id: str | None = None


class TicketPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    labels: list[str] | None = None
    assignee: str | None = None
    due_date: datetime | None = None
    acceptance_criteria: str | None = None
    lifecycle_stage: str | None = None
    parent_id: str | None = None


class CommentOut(BaseModel):
    id: str
    ticket_id: str
    author_actor_id: str
    body: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: Comment) -> CommentOut:
        return cls.model_validate(row, from_attributes=True)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=100_000)


class ActivityOut(BaseModel):
    """One activity entry: an audit row scoped to this ticket (MOD-07)."""

    id: str
    actor_id: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: AuditEvent) -> ActivityOut:
        return cls.model_validate(row, from_attributes=True)


# ------------------------------------------------------------- error mapping


def _domain(exc: TrackerNotFoundError | TrackerValidationError) -> HTTPException:
    if isinstance(exc, TrackerNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ----------------------------------------------------------------- projects


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(
    actor: ReaderActor, engine: EngineDep, workspace: str | None = None
) -> list[ProjectOut]:
    with Session(engine) as session:
        rows = _service(session, actor).list_projects(workspace_id=workspace)
        return [ProjectOut.from_row(row) for row in rows]


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, actor: WriterActor, engine: EngineDep) -> ProjectOut:
    with Session(engine) as session:
        workspace_id = body.workspace_id or _default_workspace(session)
        try:
            row = _service(session, actor).create_project(
                workspace_id=workspace_id,
                name=body.name,
                goal=body.goal,
                scope=body.scope,
                owner=body.owner,
                target_date=body.target_date,
                status=body.status,
            )
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return ProjectOut.from_row(row)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, actor: ReaderActor, engine: EngineDep) -> ProjectOut:
    with Session(engine) as session:
        try:
            return ProjectOut.from_row(_service(session, actor).get_project(project_id))
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str, body: ProjectPatch, actor: WriterActor, engine: EngineDep
) -> ProjectOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor).update_project(
                project_id, body.model_dump(exclude_unset=True)
            )
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return ProjectOut.from_row(row)


def _default_workspace(session: Session) -> str:
    workspaces = session.exec(select(Workspace)).all()
    if len(workspaces) == 1:
        return workspaces[0].id
    detail = (
        "no workspace exists yet; run `kantaq dev` once to bootstrap"
        if not workspaces
        else "multiple workspaces exist; pass workspace_id explicitly"
    )
    raise HTTPException(status_code=422, detail=detail)


# ------------------------------------------------------------------ tickets


@router.get("/tickets", response_model=list[TicketOut])
def list_tickets(
    actor: ReaderActor,
    engine: EngineDep,
    project: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    label: str | None = None,
    stage: str | None = None,
) -> list[TicketOut]:
    with Session(engine) as session:
        rows = _service(session, actor).list_tickets(
            project_id=project, status=status, assignee=assignee, label=label, stage=stage
        )
        return [TicketOut.from_row(row) for row in rows]


@router.post("/tickets", response_model=TicketOut, status_code=201)
def create_ticket(body: TicketIn, actor: WriterActor, engine: EngineDep) -> TicketOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor).create_ticket(
                project_id=body.project_id,
                title=body.title,
                description=body.description,
                status=body.status,
                priority=body.priority,
                labels=body.labels,
                assignee=body.assignee,
                due_date=body.due_date,
                acceptance_criteria=body.acceptance_criteria,
                lifecycle_stage=body.lifecycle_stage,
                parent_id=body.parent_id,
            )
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return TicketOut.from_row(row)


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, actor: ReaderActor, engine: EngineDep) -> TicketOut:
    with Session(engine) as session:
        try:
            return TicketOut.from_row(_service(session, actor).get_ticket(ticket_id))
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc


@router.patch("/tickets/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: str, body: TicketPatch, actor: WriterActor, engine: EngineDep
) -> TicketOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor).update_ticket(
                ticket_id, body.model_dump(exclude_unset=True)
            )
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return TicketOut.from_row(row)


# ----------------------------------------------------------------- comments


@router.get("/tickets/{ticket_id}/comments", response_model=list[CommentOut])
def list_comments(ticket_id: str, actor: ReaderActor, engine: EngineDep) -> list[CommentOut]:
    with Session(engine) as session:
        try:
            rows = _service(session, actor).list_comments(ticket_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        return [CommentOut.from_row(row) for row in rows]


@router.post("/tickets/{ticket_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    ticket_id: str, body: CommentIn, actor: WriterActor, engine: EngineDep
) -> CommentOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor).add_comment(ticket_id, body.body)
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return CommentOut.from_row(row)


# ----------------------------------------------------------------- activity


@router.get("/tickets/{ticket_id}/activity", response_model=list[ActivityOut])
def ticket_activity(ticket_id: str, actor: ReaderActor, engine: EngineDep) -> list[ActivityOut]:
    with Session(engine) as session:
        try:
            rows = _service(session, actor).activity(ticket_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        return [ActivityOut.from_row(row) for row in rows]


# -------------------------------------------------------------- attachments


@router.post("/tickets/{ticket_id}/attachments", response_model=TicketOut, status_code=201)
def upload_attachment(
    ticket_id: str,
    file: UploadFile,
    actor: WriterActor,
    engine: EngineDep,
    request: Request,
) -> TicketOut:
    # Read one byte past the cap: a larger upload 413s without buffering it all.
    data = file.file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"attachment exceeds the {MAX_ATTACHMENT_BYTES}-byte limit",
        )
    ref = _store(request).store(
        data,
        filename=file.filename or "attachment",
        media_type=file.content_type or "application/octet-stream",
    )
    with Session(engine) as session:
        try:
            row = _service(session, actor).add_attachment(ticket_id, ref)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        return TicketOut.from_row(row)


@router.get(
    "/tickets/{ticket_id}/attachments/{blob_id}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def download_attachment(
    ticket_id: str,
    blob_id: str,
    actor: ReaderActor,
    engine: EngineDep,
    request: Request,
) -> Response:
    """Return the raw bytes — always as an opaque, save-only attachment."""
    with Session(engine) as session:
        try:
            ticket = _service(session, actor).get_ticket(ticket_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        ref = next((a for a in ticket.attachments if a.get("blob_id") == blob_id), None)
    if ref is None:
        raise HTTPException(status_code=404, detail=f"no such attachment on this ticket: {blob_id}")
    try:
        data = _store(request).open(blob_id)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Untrusted file (PRD §15): opaque type, forced download, no sniffing.
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{ref["filename"]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
