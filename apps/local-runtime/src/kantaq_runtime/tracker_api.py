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

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from kantaq_core import context, lifecycle, reco
from kantaq_core.identity import Action, VerifiedActor
from kantaq_core.skills import DbRegistry, SkillRegistryService
from kantaq_core.telemetry import TelemetryService
from kantaq_core.tracker import (
    MAX_ATTACHMENT_BYTES,
    BlobNotFoundError,
    LocalBlobStore,
    TrackerNotFoundError,
    TrackerService,
    TrackerValidationError,
)
from kantaq_db.models import (
    AgentProposal,
    AuditEvent,
    Comment,
    EventLog,
    Project,
    Ticket,
    TicketRelationship,
    Workspace,
)
from kantaq_runtime.auth import (
    get_engine_dep,
    get_event_signer,
    require_action,
    require_human_action,
)
from kantaq_runtime.config import Settings
from kantaq_sync_engine import EventLogSink, EventSigner

router = APIRouter(prefix="/v1", tags=["tracker"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]
# Writes are human-only: agents propose through the MCP gateway, never the HTTP
# tracker API (DEBT-37 / D-27 — an over-scoped agent token is refused at the
# door, the boundary half of the issuance clamp).
WriterActor = Annotated[VerifiedActor, Depends(require_human_action(Action.tickets_write))]
# Resolved only on write routes (it ensures the member's self-grant); reads
# never carry it, so a GET never triggers a grant write (E04-T4).
SignerDep = Annotated[EventSigner | None, Depends(get_event_signer)]


def blob_store_for(settings: Settings) -> LocalBlobStore:
    """The attachment blob store: ``<db dir>/blobs`` in solo mode (D-13)."""
    return LocalBlobStore(Path(settings.local_db_path).parent / "blobs")


def _service(
    session: Session, actor: VerifiedActor, signer: EventSigner | None = None
) -> TrackerService:
    # The sink closes the MOD-03 rule "all writes go through the sync engine
    # as Events": entity row, audit row, and event-log row share one
    # transaction, attributed to the authenticated member (E04). Post-cutover
    # (E04-T4) the signer makes each emitted event Ed25519-signed; reads pass
    # no signer (None) so a GET never signs or ensures a grant.
    sink = EventLogSink(session, actor.member_id, signer=signer)
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
    # E19 (MOD-11): the per-row badges. `sync_state` is "draft" while any of
    # this ticket's events is still uncommitted (event_log.committed_rev IS
    # NULL), "committed" once the backend has acked them all.
    sync_state: str
    pending_proposals: int
    # E14 (MOD-20): the locked recommended-next rule, computed per row.
    recommended_next_stages: list[str]
    # E12-T3 (MOD-03): relation badges for the list view. `subticket_count` is
    # the direct children (parent/sub); `relationship_count` is typed edges
    # touching this ticket either way; `blocked` is true while an unresolved
    # blocker (a blocked-by/blocking edge from a not-done ticket) points at it.
    subticket_count: int
    relationship_count: int
    blocked: bool

    @classmethod
    def from_row(
        cls,
        row: Ticket,
        *,
        sync_state: str = "committed",
        pending_proposals: int = 0,
        recommended_next_stages: list[str] | None = None,
        subticket_count: int = 0,
        relationship_count: int = 0,
        blocked: bool = False,
    ) -> TicketOut:
        return cls.model_validate(
            {
                **row.model_dump(),
                "sync_state": sync_state,
                "pending_proposals": pending_proposals,
                "recommended_next_stages": recommended_next_stages or [],
                "subticket_count": subticket_count,
                "relationship_count": relationship_count,
                "blocked": blocked,
            }
        )


def _draft_ticket_ids(session: Session, ticket_ids: list[str]) -> set[str]:
    """Tickets with at least one event the backend has not committed yet."""
    if not ticket_ids:
        return set()
    statement = (
        select(EventLog.entity_id)
        .where(
            EventLog.collection == "tickets",
            col(EventLog.entity_id).in_(ticket_ids),
            col(EventLog.committed_rev).is_(None),
        )
        .distinct()
    )
    return set(session.exec(statement).all())


def _pending_proposal_counts(session: Session, ticket_ids: list[str]) -> dict[str, int]:
    if not ticket_ids:
        return {}
    statement = (
        select(AgentProposal.ticket_id, func.count())
        .where(
            AgentProposal.status == "pending",
            col(AgentProposal.ticket_id).in_(ticket_ids),
        )
        .group_by(col(AgentProposal.ticket_id))
    )
    return dict(session.exec(statement).all())


def _open_subticket_parents(session: Session, ticket_ids: list[str]) -> set[str]:
    """The tickets among ``ticket_ids`` that still have open (not-done) children."""
    if not ticket_ids:
        return set()
    statement = (
        select(Ticket.parent_id)
        .where(col(Ticket.parent_id).in_(ticket_ids), Ticket.status != "done")
        .distinct()
    )
    return {parent for parent in session.exec(statement).all() if parent is not None}


def _subticket_counts(session: Session, ticket_ids: list[str]) -> dict[str, int]:
    """Direct-child counts per parent (the parent/sub badge), one query."""
    if not ticket_ids:
        return {}
    statement = (
        select(Ticket.parent_id, func.count())
        .where(col(Ticket.parent_id).in_(ticket_ids))
        .group_by(col(Ticket.parent_id))
    )
    return {parent: n for parent, n in session.exec(statement).all() if parent is not None}


def _relationship_counts(session: Session, ticket_ids: list[str]) -> dict[str, int]:
    """Typed-edge counts per ticket (either endpoint), one query."""
    if not ticket_ids:
        return {}
    ids = set(ticket_ids)
    statement = select(TicketRelationship).where(
        col(TicketRelationship.from_id).in_(ids) | col(TicketRelationship.to_id).in_(ids)
    )
    counts: dict[str, int] = {}
    for rel in session.exec(statement).all():
        if rel.from_id in ids:
            counts[rel.from_id] = counts.get(rel.from_id, 0) + 1
        if rel.to_id in ids:
            counts[rel.to_id] = counts.get(rel.to_id, 0) + 1
    return counts


def _blocked_ids(session: Session, ticket_ids: list[str]) -> set[str]:
    """Tickets with an unresolved blocker — a blocked-by/blocking edge whose
    blocking ticket is not yet ``done`` (two batched queries, no N+1)."""
    if not ticket_ids:
        return set()
    ids = set(ticket_ids)
    statement = select(TicketRelationship).where(
        col(TicketRelationship.type).in_(("blocking", "blocked-by")),
        col(TicketRelationship.from_id).in_(ids) | col(TicketRelationship.to_id).in_(ids),
    )
    # (blocked ticket in this list, the ticket blocking it)
    pairs: list[tuple[str, str]] = []
    blocker_ids: set[str] = set()
    for rel in session.exec(statement).all():
        if rel.type == "blocking" and rel.to_id in ids:
            pairs.append((rel.to_id, rel.from_id))
            blocker_ids.add(rel.from_id)
        elif rel.type == "blocked-by" and rel.from_id in ids:
            pairs.append((rel.from_id, rel.to_id))
            blocker_ids.add(rel.to_id)
    if not blocker_ids:
        return set()
    statuses = dict(
        session.exec(select(Ticket.id, Ticket.status).where(col(Ticket.id).in_(blocker_ids))).all()
    )
    return {blocked for blocked, blocker in pairs if statuses.get(blocker) != "done"}


def _recommended_next(row: Ticket, open_parents: set[str]) -> list[str]:
    if not lifecycle.is_stage(row.lifecycle_stage):
        return []  # legacy row predating the locked taxonomy (fail-soft, MOD-20)
    return list(
        lifecycle.recommend_next(row.lifecycle_stage, has_open_subtickets=row.id in open_parents)
    )


def tickets_out(session: Session, rows: list[Ticket]) -> list[TicketOut]:
    """Decorate ticket rows with sync + proposal badges, the recommended next
    stages, and the E12 relation badges — all via batched queries, no N+1
    (MOD-11 + MOD-20 + MOD-03)."""
    ids = [row.id for row in rows]
    drafts = _draft_ticket_ids(session, ids)
    counts = _pending_proposal_counts(session, ids)
    open_parents = _open_subticket_parents(session, ids)
    subtickets = _subticket_counts(session, ids)
    relationships = _relationship_counts(session, ids)
    blocked = _blocked_ids(session, ids)
    return [
        TicketOut.from_row(
            row,
            sync_state="draft" if row.id in drafts else "committed",
            pending_proposals=counts.get(row.id, 0),
            recommended_next_stages=_recommended_next(row, open_parents),
            subticket_count=subtickets.get(row.id, 0),
            relationship_count=relationships.get(row.id, 0),
            blocked=row.id in blocked,
        )
        for row in rows
    ]


def ticket_out(session: Session, row: Ticket) -> TicketOut:
    return tickets_out(session, [row])[0]


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
    # Fail closed on unknown fields: this shape also coerces agent-proposal
    # diffs at approve time (MOD-12), where a silently-dropped key would be a
    # silent bypass if the dump ever stopped flowing through the tracker's
    # own field validation (SEC second review).
    model_config = ConfigDict(extra="forbid")

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


class LifecycleStageOut(BaseModel):
    """One locked lifecycle stage (MOD-20): identity, labels, canonical order."""

    slug: str
    title: str
    purpose: str
    containers: list[str]
    order: int


class RelationOut(BaseModel):
    """One typed ticket relationship (E12-T3), seen from a ticket's side.

    ``direction`` is relative to the ticket in the request path: ``outgoing``
    when it is the ``from`` end (e.g. it *blocks* the other), ``incoming`` when
    it is the ``to`` end (e.g. it *is blocked by* the other). The raw stored
    edge (``from_id``/``to_id``/``type``) is always one of the five real types —
    no inverse pseudo-type is invented for the view.
    """

    id: str
    from_id: str
    to_id: str
    type: str
    direction: str
    created_by: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: TicketRelationship, *, ticket_id: str) -> RelationOut:
        return cls.model_validate(
            {
                **row.model_dump(),
                "direction": "outgoing" if row.from_id == ticket_id else "incoming",
            }
        )


class RelationIn(BaseModel):
    to_id: str
    type: str


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
def create_project(
    body: ProjectIn, actor: WriterActor, engine: EngineDep, signer: SignerDep
) -> ProjectOut:
    with Session(engine) as session:
        workspace_id = body.workspace_id or _default_workspace(session)
        try:
            row = _service(session, actor, signer).create_project(
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
    project_id: str,
    body: ProjectPatch,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> ProjectOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).update_project(
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
    parent: str | None = None,
) -> list[TicketOut]:
    with Session(engine) as session:
        rows = _service(session, actor).list_tickets(
            project_id=project,
            status=status,
            assignee=assignee,
            label=label,
            stage=stage,
            parent=parent,
        )
        return tickets_out(session, rows)


@router.post("/tickets", response_model=TicketOut, status_code=201)
def create_ticket(
    body: TicketIn, actor: WriterActor, engine: EngineDep, signer: SignerDep
) -> TicketOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).create_ticket(
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
        return ticket_out(session, row)


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, actor: ReaderActor, engine: EngineDep) -> TicketOut:
    with Session(engine) as session:
        try:
            return ticket_out(session, _service(session, actor).get_ticket(ticket_id))
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc


class RecommendationOut(BaseModel):
    """The MOD-22 recommendation contract (FR-E17-1), one per (ticket, container)."""

    role: str
    skill_container: str
    why: str
    required_memory: list[str]
    missing_memory: list[str]
    expected_output: str
    mapped_tool: str
    mcp_session_template: str
    risk_level: str
    confidence: str
    approval_rule: str

    @classmethod
    def from_rec(cls, rec: reco.Recommendation) -> RecommendationOut:
        return cls(
            role=rec.role,
            skill_container=rec.skill_container,
            why=rec.why,
            required_memory=list(rec.required_memory),
            missing_memory=list(rec.missing_memory),
            expected_output=rec.expected_output,
            mapped_tool=rec.mapped_tool,
            mcp_session_template=rec.mcp_session_template,
            risk_level=rec.risk_level,
            confidence=rec.confidence,
            approval_rule=rec.approval_rule,
        )


@router.get("/tickets/{ticket_id}/recommendations", response_model=list[RecommendationOut])
def get_recommendations(
    ticket_id: str, actor: ReaderActor, engine: EngineDep
) -> list[RecommendationOut]:
    """Structured role/skill recommendations for a ticket (E17-T1, FR-E17-1).

    The rule engine (MOD-22) is keyed on the ticket's lifecycle stage + label
    signals; each recommendation's ``missing_memory`` is resolved per role through
    the MOD-21 resolver, so the panel can show "this role wants codebase context
    and there is none linked". Read-only: needs ``tickets.read``.
    """
    with Session(engine) as session:
        try:
            ticket = _service(session, actor).get_ticket(ticket_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc

        now = datetime.now(UTC)
        missing_by_role: dict[str, tuple[str, ...]] = {}

        def missing_memory_for(role: str) -> tuple[str, ...]:
            if role not in missing_by_role:
                bundle = context.resolve_for_ticket(
                    session, ticket, role, actor_id=actor.member_id, now=now
                )
                missing_by_role[role] = bundle.missing
            return missing_by_role[role]

        # E17-T5 (FR-E17-2): read the db-backed registry so a user's container
        # edits + skill→tool mappings reflect in the output. A runtime whose db
        # is unseeded (no migration 0010 rows) falls back to the pure hardcoded
        # tuple, so recommendations never go empty on a fresh replica.
        registry_svc = SkillRegistryService(session, actor_id=actor.member_id)
        containers = registry_svc.list_containers()
        if containers:
            registry = DbRegistry(containers, registry_svc.list_mappings())
            recs = reco.recommend(ticket, registry=registry, missing_memory_for=missing_memory_for)
        else:
            recs = reco.recommend(ticket, missing_memory_for=missing_memory_for)
        return [RecommendationOut.from_rec(rec) for rec in recs]


@router.patch("/tickets/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: str,
    body: TicketPatch,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> TicketOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).update_ticket(
                ticket_id, body.model_dump(exclude_unset=True)
            )
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return ticket_out(session, row)


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
    ticket_id: str,
    body: CommentIn,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> CommentOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).add_comment(ticket_id, body.body)
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return CommentOut.from_row(row)


# ----------------------------------------------------- relations (E12-T3)


@router.get("/tickets/{ticket_id}/relations", response_model=list[RelationOut])
def list_relations(ticket_id: str, actor: ReaderActor, engine: EngineDep) -> list[RelationOut]:
    """Every typed relationship touching the ticket, from either end."""
    with Session(engine) as session:
        try:
            rows = _service(session, actor).relations_for(ticket_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        return [RelationOut.from_row(row, ticket_id=ticket_id) for row in rows]


@router.post("/tickets/{ticket_id}/relations", response_model=RelationOut, status_code=201)
def create_relation(
    ticket_id: str,
    body: RelationIn,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> RelationOut:
    with Session(engine) as session:
        try:
            row = _service(session, actor, signer).add_relation(ticket_id, body.to_id, body.type)
        except (TrackerNotFoundError, TrackerValidationError) as exc:
            raise _domain(exc) from exc
        return RelationOut.from_row(row, ticket_id=ticket_id)


@router.delete("/tickets/{ticket_id}/relations/{relationship_id}", status_code=204)
def delete_relation(
    ticket_id: str,
    relationship_id: str,
    actor: WriterActor,
    engine: EngineDep,
    signer: SignerDep,
) -> None:
    with Session(engine) as session:
        try:
            service = _service(session, actor, signer)
            # Scope the delete to the path ticket: the relationship must touch
            # it, so a mismatched id is a 404 rather than a cross-ticket delete.
            if not any(r.id == relationship_id for r in service.relations_for(ticket_id)):
                raise TrackerNotFoundError("ticket_relationships", relationship_id)
            service.remove_relation(relationship_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc


# ---------------------------------------------------------- lifecycle (MOD-20)


@router.get("/lifecycle/stages", response_model=list[LifecycleStageOut])
def lifecycle_stages(actor: ReaderActor) -> list[LifecycleStageOut]:
    """The locked 9-stage taxonomy (E14), so UIs and agents render it
    without hardcoding slugs, titles, or skill containers."""
    return [
        LifecycleStageOut(
            slug=stage.slug,
            title=stage.title,
            purpose=stage.purpose,
            containers=list(stage.containers),
            order=index,
        )
        for index, stage in enumerate(lifecycle.stages())
    ]


# ----------------------------------------------------------------- activity


@router.get("/tickets/{ticket_id}/activity", response_model=list[ActivityOut])
def ticket_activity(ticket_id: str, actor: ReaderActor, engine: EngineDep) -> list[ActivityOut]:
    with Session(engine) as session:
        try:
            rows = _service(session, actor).activity(ticket_id)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        # Telemetry (E28, opt-in no-op): audit-query frequency — a count only.
        if TelemetryService(session).record("activity_viewed", {"count": len(rows)}):
            session.commit()
        return [ActivityOut.from_row(row) for row in rows]


# -------------------------------------------------------------- attachments


@router.post("/tickets/{ticket_id}/attachments", response_model=TicketOut, status_code=201)
def upload_attachment(
    ticket_id: str,
    file: UploadFile,
    actor: WriterActor,
    engine: EngineDep,
    request: Request,
    signer: SignerDep,
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
            row = _service(session, actor, signer).add_attachment(ticket_id, ref)
        except TrackerNotFoundError as exc:
            raise _domain(exc) from exc
        return ticket_out(session, row)


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
